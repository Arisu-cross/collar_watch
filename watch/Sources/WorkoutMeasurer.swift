import Foundation
import HealthKit
import WatchKit

// workout session 实测。Apple SpeedySloth 标准路线:
// HKWorkoutSession + HKLiveWorkoutBuilder,短时高频心率采样(每几秒一个),
// 结束 endCollection → discardWorkout——训练记录不落库,健身三环零污染;
// 心率样本本身照常写入 HealthKit,由增量上报链路自动带走。
// 只由服务端指令启动(Scheduler.checkCommand 前台分支),无指令永不开。
final class WorkoutMeasurer: NSObject, ObservableObject,
                             HKWorkoutSessionDelegate, HKLiveWorkoutBuilderDelegate {
    static let shared = WorkoutMeasurer()

    @Published var measuring = false
    @Published var currentHeartRate: Double = 0
    @Published var secondsLeft: Int = 0

    private var session: HKWorkoutSession?
    private var builder: HKLiveWorkoutBuilder?
    private var readings: [Double] = []
    private var commandID = ""
    private var duration = 30

    func start(commandID: String, duration: Int = 30) {
        guard !measuring else { return }
        self.commandID = commandID
        self.duration = max(10, min(duration, 300))   // 服务端下发,10-300s 夹紧
        readings = []
        let cfg = HKWorkoutConfiguration()
        cfg.activityType = .other
        cfg.locationType = .indoor
        let store = HealthCollector.shared.store
        do {
            let s = try HKWorkoutSession(healthStore: store, configuration: cfg)
            let b = s.associatedWorkoutBuilder()
            b.dataSource = HKLiveWorkoutDataSource(healthStore: store,
                                                   workoutConfiguration: cfg)
            s.delegate = self
            b.delegate = self
            session = s
            builder = b
            DispatchQueue.main.async {
                self.measuring = true
                self.secondsLeft = self.duration
            }
            s.startActivity(with: Date())
            b.beginCollection(withStart: Date()) { _, _ in }
            Task { await self.countdownAndFinish() }
        } catch {
            Status.shared.note(failure: -1, message: "workout: \(error.localizedDescription)")
        }
    }

    private func countdownAndFinish() async {
        for i in stride(from: duration, to: 0, by: -1) {
            await MainActor.run { self.secondsLeft = i }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        await finish()
    }

    private func finish() async {
        // 倒计时归零即视为测量完成:先收起 UI 并放下 measuring 旗,
        // 之后 session 收尾期的杂音会被 didFailWithError 的门挡掉,不上脸
        await MainActor.run {
            self.measuring = false
            self.currentHeartRate = 0
        }
        // 本地记账在最前:这条指令已执行,重进 app 也绝不重测
        CommandFetcher.markCompleted(commandID)
        // 回执趁 workout session 还活着(后台执行权在手)先发——
        // session 一关 app 立刻可能被系统挂起,回执会冻在半路
        let result: [String: Any]
        if readings.isEmpty {
            result = ["sample_count": 0]
        } else {
            result = [
                "heart_rate_average": (readings.reduce(0, +) / Double(readings.count)).rounded(),
                "heart_rate_minimum": readings.min()!.rounded(),
                "heart_rate_maximum": readings.max()!.rounded(),
                "sample_count": readings.count,
            ]
        }
        await CommandFetcher.postResult(commandID: commandID, result: result)
        WKInterfaceDevice.current().play(.success)   // 轻震 = 结果已回传
        session?.end()
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            guard let b = builder else { cont.resume(); return }
            b.endCollection(withEnd: Date()) { _, _ in
                b.discardWorkout()   // 不保存训练记录,三环零污染
                cont.resume()
            }
        }
        // workout 期间写入 HealthKit 的心率样本交给增量上报
        // (background URLSession 系统保送,app 挂起也送得完)
        await Scheduler.runCycle(foreground: false)
        session = nil
        builder = nil
    }

    // MARK: - HKLiveWorkoutBuilderDelegate
    func workoutBuilder(_ workoutBuilder: HKLiveWorkoutBuilder,
                        didCollectDataOf collectedTypes: Set<HKSampleType>) {
        guard let hrType = HKQuantityType.quantityType(forIdentifier: .heartRate),
              collectedTypes.contains(hrType),
              let stats = workoutBuilder.statistics(for: hrType),
              let q = stats.mostRecentQuantity() else { return }
        let bpm = q.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
        readings.append(bpm)
        DispatchQueue.main.async { self.currentHeartRate = bpm }
    }

    func workoutBuilderDidCollectEvent(_ workoutBuilder: HKLiveWorkoutBuilder) {}

    // MARK: - HKWorkoutSessionDelegate
    func workoutSession(_ workoutSession: HKWorkoutSession,
                        didChangeTo toState: HKWorkoutSessionState,
                        from fromState: HKWorkoutSessionState, date: Date) {}

    func workoutSession(_ workoutSession: HKWorkoutSession,
                        didFailWithError error: Error) {
        // 门:只有测量进行中的真失败才亮红字;收尾/完成后的杂音一律静音
        guard measuring else { return }
        Status.shared.note(failure: -1, message: "workout: \(error.localizedDescription)")
        DispatchQueue.main.async { self.measuring = false }
    }
}
