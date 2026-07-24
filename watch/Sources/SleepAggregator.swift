import Foundation
import HealthKit

// 睡眠线:每次全量查最近 48h 分段 → 聚合 session → 全部上报,靠服务端
// (type, at) 幂等去重。不走 anchor:分段分批到达时 anchor 会把半截觉
// 永久锁死,全量+幂等以极小的重复上传换正确性。
// 回笼觉 = 间隔>60min 的新 session = 新的一条,天然覆盖。
enum SleepAggregator {
    static let sessionGap: TimeInterval = 60 * 60
    static let settleTime: TimeInterval = 10 * 60   // 只聚合"停笔"超过 10 分钟的觉,防半截

    static func collect(store: HKHealthStore) async -> [Sample] {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return [] }
        let start = Date().addingTimeInterval(-48 * 3600)
        let pred = HKQuery.predicateForSamples(withStart: start, end: nil)
        let rows: [HKCategorySample] = await withCheckedContinuation { cont in
            let q = HKSampleQuery(sampleType: type, predicate: pred, limit: HKObjectQueryNoLimit,
                                  sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate,
                                                                     ascending: true)]) { _, rows, _ in
                cont.resume(returning: (rows as? [HKCategorySample]) ?? [])
            }
            store.execute(q)
        }
        return aggregate(rows: rows, now: Date())
    }

    static func aggregate(rows: [HKCategorySample], now: Date) -> [Sample] {
        // 手表只产 asleep*/awake 分段;inBed 是 iPhone 侧概念,丢掉
        let stages = rows.filter { $0.value != HKCategoryValueSleepAnalysis.inBed.rawValue }
        guard !stages.isEmpty else { return [] }

        var sessions: [[HKCategorySample]] = []
        var current: [HKCategorySample] = []
        for s in stages {
            if let last = current.last, s.startDate.timeIntervalSince(last.endDate) > sessionGap {
                sessions.append(current); current = []
            }
            current.append(s)
        }
        if !current.isEmpty { sessions.append(current) }

        let iso = ISO8601DateFormatter()
        var out: [Sample] = []
        for session in sessions {
            guard let first = session.first, let last = session.last else { continue }
            guard now.timeIntervalSince(last.endDate) > settleTime else { continue }
            var dur: [Int: TimeInterval] = [:]
            for s in session {
                dur[s.value, default: 0] += s.endDate.timeIntervalSince(s.startDate)
            }
            func hours(_ v: HKCategoryValueSleepAnalysis) -> Double {
                ((dur[v.rawValue] ?? 0) / 3600 * 1000).rounded() / 1000
            }
            let core = hours(.asleepCore), deep = hours(.asleepDeep), rem = hours(.asleepREM)
            let unspecified = hours(.asleepUnspecified)
            let total = core + deep + rem + unspecified
            guard total > 0 else { continue }
            // 分段时间轴:合并相邻同阶段成块(哪段几点到几点深睡/REM/醒)
            let stageName: [Int: String] = [
                HKCategoryValueSleepAnalysis.asleepDeep.rawValue: "deep",
                HKCategoryValueSleepAnalysis.asleepCore.rawValue: "core",
                HKCategoryValueSleepAnalysis.asleepREM.rawValue: "rem",
                HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue: "asleep",
                HKCategoryValueSleepAnalysis.awake.rawValue: "awake",
            ]
            var segments: [[String: String]] = []
            for s in session {
                let name = stageName[s.value] ?? "other"
                if var seg = segments.last, seg["stage"] == name,
                   let e = iso.date(from: seg["end"] ?? ""),
                   s.startDate.timeIntervalSince(e) < 60 {
                    seg["end"] = iso.string(from: s.endDate)
                    segments[segments.count - 1] = seg
                } else {
                    segments.append(["stage": name,
                                     "start": iso.string(from: s.startDate),
                                     "end": iso.string(from: s.endDate)])
                }
            }
            out.append(Sample(
                type: "sleep_analysis", value: nil, unit: "hr", at: last.endDate,
                extra: [
                    "totalSleep": total, "core": core, "deep": deep, "rem": rem,
                    "awake": hours(.awake),
                    "sleepStart": iso.string(from: first.startDate),
                    "sleepEnd": iso.string(from: last.endDate),
                    "segments": segments,
                ]))
        }
        return out
    }
}
