import Foundation
import HealthKit

// 一条待上报样本。extra 仅睡眠聚合用(分段汇总),数值类为 nil。
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

    // 数据全走手表(HAE 延迟太高、且要手机亮屏)。累计类一并采集。
    // 双计红线:关 HAE 前手表也推累计=翻倍;切换当天库里 HAE 残留会和手表叠加,
    // 明天起纯手表干净。距离用 mile 匹配 HAE 单位,免今日总量 sum 时单位混。
    private let quantityTypes: [(HKQuantityTypeIdentifier, String, HKUnit, String)] = [
        (.heartRate, "heart_rate", HKUnit(from: "count/min"), "count/min"),
        (.heartRateVariabilitySDNN, "heart_rate_variability", .secondUnit(with: .milli), "ms"),
        (.restingHeartRate, "resting_heart_rate", HKUnit(from: "count/min"), "count/min"),
        (.respiratoryRate, "respiratory_rate", HKUnit(from: "count/min"), "count/min"),
        // 腕温:SE3 睡眠期采集,每晚一两条。命名与 HAE 同款,两源自动合流。
        (.appleSleepingWristTemperature, "apple_sleeping_wrist_temperature", .degreeCelsius(), "degC"),
        // 累计类(今日总量=服务端 sum 当天样本)。只有手表账,不戴表时段会缺。
        (.stepCount, "step_count", .count(), "count"),
        (.distanceWalkingRunning, "walking_running_distance", .mile(), "mi"),
        (.flightsClimbed, "flights_climbed", .count(), "count"),
        (.activeEnergyBurned, "active_energy_burned", .kilocalorie(), "kcal"),
        (.appleExerciseTime, "apple_exercise_time", .minute(), "min"),
    ]

    var readTypes: Set<HKObjectType> {
        var t = Set(quantityTypes.compactMap { HKObjectType.quantityType(forIdentifier: $0.0) as HKObjectType? })
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
        for (id, name, unit, unitLabel) in quantityTypes {
            guard let qt = HKObjectType.quantityType(forIdentifier: id) else { continue }
            let (rows, newAnchor) = await queryAnchored(type: qt)
            for s in rows {
                guard let qs = s as? HKQuantitySample else { continue }
                samples.append(Sample(type: name, value: qs.quantity.doubleValue(for: unit),
                                      unit: unitLabel, at: qs.endDate))
            }
            if let newAnchor,
               let data = try? NSKeyedArchiver.archivedData(withRootObject: newAnchor,
                                                            requiringSecureCoding: true) {
                anchors[name] = data
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
        for (id, name, _, _) in quantityTypes
        where HKObjectType.quantityType(forIdentifier: id) == type { return name }
        return type.identifier
    }
}
