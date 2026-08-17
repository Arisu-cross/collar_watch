import Foundation
import HealthKit

// 一条待上报样本。extra 给结构化样本用(睡眠分段汇总、经期流量标签),
// 纯数值类为 nil。
struct Sample {
    let type: String
    let value: Double?
    let unit: String
    let at: Date
    var extra: [String: Any]? = nil

    func asJSON(iso: ISO8601DateFormatter) -> [String: Any] {
        var d: [String: Any] = ["type": type, "unit": unit, "at": iso.string(from: at)]
        d["value"] = value ?? NSNull()
        if let extra { d["extra"] = extra }
        return d
    }
}

// 点测类增量采集。游标(anchor)只在上报成功后由 Uploader 回调提交——
// 失败不前进,样本反正躺在健康库里,下次重查自动重传,服务端 (type,at) 幂等。
final class HealthCollector {
    static let shared = HealthCollector()
    let store = HKHealthStore()

    // 数值类采集口径。scale = HealthKit 单位换算成上报单位的倍率
    // (血氧 HealthKit 给 0–1 的比例,上报按 % 与 HAE 对齐)。
    private struct QuantitySpec {
        let id: HKQuantityTypeIdentifier
        let name: String
        let unit: HKUnit
        let unitLabel: String
        var scale: Double = 1
    }

    // 分类类采集口径(非数值,靠 extra 带标签)。
    private struct CategorySpec {
        let id: HKCategoryTypeIdentifier
        let name: String
    }

    // 数据全走手表(HAE 延迟太高、且要手机亮屏)。累计类一并采集。
    // 双计红线:关 HAE 前手表也推累计=翻倍;切换当天库里 HAE 残留会和手表叠加,
    // 明天起纯手表干净。距离用 mile 匹配 HAE 单位,免今日总量 sum 时单位混。
    private let quantityTypes: [QuantitySpec] = [
        .init(id: .heartRate, name: "heart_rate", unit: HKUnit(from: "count/min"), unitLabel: "count/min"),
        .init(id: .heartRateVariabilitySDNN, name: "heart_rate_variability", unit: .secondUnit(with: .milli), unitLabel: "ms"),
        .init(id: .restingHeartRate, name: "resting_heart_rate", unit: HKUnit(from: "count/min"), unitLabel: "count/min"),
        .init(id: .respiratoryRate, name: "respiratory_rate", unit: HKUnit(from: "count/min"), unitLabel: "count/min"),
        // 血氧:只有带血氧传感器的机型才产样本(SE 系列没有;部分地区/系统版本
        // 该功能被关掉也会一直空)。查不到数据不是 bug,服务端照旧空着。
        .init(id: .oxygenSaturation, name: "blood_oxygen_saturation", unit: .percent(), unitLabel: "%", scale: 100),
        // 腕温:SE3 睡眠期采集,每晚一两条。命名与 HAE 同款,两源自动合流。
        .init(id: .appleSleepingWristTemperature, name: "apple_sleeping_wrist_temperature", unit: .degreeCelsius(), unitLabel: "degC"),
        // 音量暴露:系统按时段写聚合样本(每条自带一个区间均值),不是累计量,
        // 服务端按"今日最大/均值"呈现而不是求和。耳机音量只在戴 AirPods 等
        // 支持的设备放音时才有;环境音量靠手表麦克风,关掉"环境音量测量"就没有。
        .init(id: .environmentalAudioExposure, name: "environmental_audio_exposure", unit: .decibelAWeightedSoundPressureLevel(), unitLabel: "dBASPL"),
        .init(id: .headphoneAudioExposure, name: "headphone_audio_exposure", unit: .decibelAWeightedSoundPressureLevel(), unitLabel: "dBASPL"),
        // 累计类(今日总量=服务端 sum 当天样本)。只有手表账,不戴表时段会缺。
        .init(id: .stepCount, name: "step_count", unit: .count(), unitLabel: "count"),
        .init(id: .distanceWalkingRunning, name: "walking_running_distance", unit: .mile(), unitLabel: "mi"),
        .init(id: .flightsClimbed, name: "flights_climbed", unit: .count(), unitLabel: "count"),
        .init(id: .activeEnergyBurned, name: "active_energy_burned", unit: .kilocalorie(), unitLabel: "kcal"),
        .init(id: .appleExerciseTime, name: "apple_exercise_time", unit: .minute(), unitLabel: "min"),
    ]

    // 经期:手表/手机的「经期跟踪」写进 HealthKit 的分类样本,一天一条。
    // at 取 startDate(那一天),流量档位放 value,可读标签与「周期开始」放 extra。
    private let categoryTypes: [CategorySpec] = [
        .init(id: .menstrualFlow, name: "menstrual_flow"),
    ]

    // HKCategoryValueMenstrualFlow:1 unspecified / 2 light / 3 medium / 4 heavy / 5 none。
    // `.none` 与 Optional.none 撞名,那一档直接写字面量,别去碰它。
    private static let menstrualFlowNames: [Int: String] = [
        HKCategoryValueMenstrualFlow.unspecified.rawValue: "unspecified",
        HKCategoryValueMenstrualFlow.light.rawValue: "light",
        HKCategoryValueMenstrualFlow.medium.rawValue: "medium",
        HKCategoryValueMenstrualFlow.heavy.rawValue: "heavy",
        5: "none",
    ]

    var readTypes: Set<HKObjectType> {
        var t = Set(quantityTypes.compactMap { HKObjectType.quantityType(forIdentifier: $0.id) as HKObjectType? })
        for spec in categoryTypes {
            if let ct = HKObjectType.categoryType(forIdentifier: spec.id) { t.insert(ct) }
        }
        t.insert(HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!)
        return t
    }

    func requestAuthorization() async throws {
        // workout 写权限是 HKLiveWorkoutBuilder.beginCollection 的门票(实时测量用)
        try await store.requestAuthorization(toShare: [HKObjectType.workoutType()],
                                             read: readTypes)
    }

    // 返回 (新样本, 待提交游标)。游标编码后交 Uploader,200 后 commit。
    func collect() async -> (samples: [Sample], pendingAnchors: [String: Data]) {
        var samples: [Sample] = []
        var anchors: [String: Data] = [:]
        for spec in quantityTypes {
            guard let qt = HKObjectType.quantityType(forIdentifier: spec.id) else { continue }
            let (rows, newAnchor) = await queryAnchored(type: qt)
            for s in rows {
                guard let qs = s as? HKQuantitySample else { continue }
                samples.append(Sample(type: spec.name,
                                      value: qs.quantity.doubleValue(for: spec.unit) * spec.scale,
                                      unit: spec.unitLabel, at: qs.endDate))
            }
            if let newAnchor,
               let data = try? NSKeyedArchiver.archivedData(withRootObject: newAnchor,
                                                            requiringSecureCoding: true) {
                anchors[spec.name] = data
            }
        }
        for spec in categoryTypes {
            guard let ct = HKObjectType.categoryType(forIdentifier: spec.id) else { continue }
            let (rows, newAnchor) = await queryAnchored(type: ct)
            for s in rows {
                guard let cs = s as? HKCategorySample else { continue }
                var extra: [String: Any] = [:]
                if spec.id == .menstrualFlow {
                    extra["flow"] = Self.menstrualFlowNames[cs.value] ?? "unspecified"
                    // 这条是不是一次周期的第一天(健康 app 里勾的那个)
                    extra["cycle_start"] = (cs.metadata?[HKMetadataKeyMenstrualCycleStart] as? Bool) ?? false
                }
                samples.append(Sample(type: spec.name, value: Double(cs.value), unit: "",
                                      at: cs.startDate, extra: extra.isEmpty ? nil : extra))
            }
            if let newAnchor,
               let data = try? NSKeyedArchiver.archivedData(withRootObject: newAnchor,
                                                            requiringSecureCoding: true) {
                anchors[spec.name] = data
            }
        }
        return (samples, anchors)
    }

    static func commit(anchors: [String: Data]) {
        let ud = UserDefaults.standard
        for (name, data) in anchors { ud.set(data, forKey: "anchor.\(name)") }
    }

    private func savedAnchor(for name: String) -> HKQueryAnchor? {
        guard let data = UserDefaults.standard.data(forKey: "anchor.\(name)") else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    private func queryAnchored(type: HKSampleType) async -> ([HKSample], HKQueryAnchor?) {
        let name = shortName(for: type)
        return await withCheckedContinuation { cont in
            let q = HKAnchoredObjectQuery(type: type, predicate: nil,
                                          anchor: savedAnchor(for: name),
                                          limit: HKObjectQueryNoLimit) { _, rows, _, newAnchor, _ in
                cont.resume(returning: (rows ?? [], newAnchor))
            }
            store.execute(q)
        }
    }

    private func shortName(for type: HKSampleType) -> String {
        for spec in quantityTypes
        where HKObjectType.quantityType(forIdentifier: spec.id) == type { return spec.name }
        for spec in categoryTypes
        where HKObjectType.categoryType(forIdentifier: spec.id) == type { return spec.name }
        return type.identifier
    }
}
