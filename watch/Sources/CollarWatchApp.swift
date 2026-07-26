import SwiftUI
import HealthKit

@main
struct CollarWatchApp: App {
    @WKApplicationDelegateAdaptor(ExtensionDelegate.self) var delegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup { StatusView() }
            .onChange(of: scenePhase) { _, phase in
                // 抬腕/打开 app 一到前台就立即上报一次,并重排后台链。
                // 抬腕即催:想刷新数据,看一眼手表就行。
                if phase == .active {
                    Scheduler.scheduleNext()
                    Task { await Scheduler.runCycle() }
                }
            }
    }
}

// 运行状态,给 UI 看 + UserDefaults 持久化(app 被杀后重启仍能看到上次战果)
final class Status: ObservableObject {
    static let shared = Status()
    @Published var lastSuccess: Date?
    @Published var lastBatchSize: Int = 0
    @Published var lastFailure: String?
    @Published var lastBackgroundWake: Date?
    @Published var authorized = false

    private init() {
        let ud = UserDefaults.standard
        lastSuccess = ud.object(forKey: "st.lastSuccess") as? Date
        lastBatchSize = ud.integer(forKey: "st.lastBatch")
        lastFailure = ud.string(forKey: "st.lastFailure")
        lastBackgroundWake = ud.object(forKey: "st.lastBgWake") as? Date
    }

    // 定时链专用观测:后台被系统叫醒的时刻。真机上这行时间在动=定时导出活着。
    func noteBackgroundWake(_ at: Date) {
        DispatchQueue.main.async {
            self.lastBackgroundWake = at
            UserDefaults.standard.set(at, forKey: "st.lastBgWake")
        }
    }

    func note(sent count: Int) {
        DispatchQueue.main.async {
            self.lastBatchSize = count
            UserDefaults.standard.set(count, forKey: "st.lastBatch")
        }
    }

    func note(success at: Date) {
        DispatchQueue.main.async {
            self.lastSuccess = at
            self.lastFailure = nil
            let ud = UserDefaults.standard
            ud.set(at, forKey: "st.lastSuccess")
            ud.removeObject(forKey: "st.lastFailure")
        }
    }

    func note(failure code: Int, message: String?) {
        DispatchQueue.main.async {
            self.lastFailure = code > 0 ? "HTTP \(code)" : (message ?? "未知错误")
            UserDefaults.standard.set(self.lastFailure, forKey: "st.lastFailure")
        }
    }
}

struct StatusView: View {
    @StateObject private var status = Status.shared
    @StateObject private var measurer = WorkoutMeasurer.shared
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(spacing: 6) {
                HStack(spacing: 4) {
                    Image(systemName: "heart.circle.fill").foregroundStyle(.pink)
                    Text("Collar").font(.footnote.weight(.semibold))
                }

                // 服务端下过测量指令时自动进入,佩戴者零操作
                if measurer.measuring {
                    VStack(spacing: 2) {
                        Image(systemName: "heart.fill")
                            .foregroundStyle(.pink)
                            .symbolEffect(.pulse)
                        Text(measurer.currentHeartRate > 0
                             ? "\(Int(measurer.currentHeartRate)) bpm" : "…")
                            .font(.title3.weight(.semibold))
                        Text("测量中 · 还剩 \(measurer.secondsLeft)s")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)
                }

                Group {
                    if let at = status.lastSuccess {
                        Text("上次 \(at.formatted(date: .omitted, time: .shortened)) · \(status.lastBatchSize) 条")
                    } else {
                        Text("还没有成功上报过")
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)

                if let wake = status.lastBackgroundWake {
                    Text("自动醒来 \(wake.formatted(date: .omitted, time: .shortened))")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if let f = status.lastFailure {
                    Text(f).font(.caption2).foregroundStyle(.red)
                }

                Button(busy ? "上报中…" : "立即上报") {
                    busy = true
                    Task {
                        Scheduler.scheduleNext()
                        await Scheduler.runCycle()
                        busy = false
                    }
                }
                .disabled(busy)

                Button(status.authorized ? "已请求授权 ✓" : "健康数据授权") {
                    Task {
                        do {
                            try await HealthCollector.shared.requestAuthorization()
                            status.authorized = true
                        } catch {
                            status.note(failure: -1, message: error.localizedDescription)
                        }
                    }
                }
                .font(.footnote)
            }
        }
        .task {
            // 前台轻轮询:app 开着时每 15 秒问一次指令。
            // 没有这个循环,后台任务先把指令标"已见"却不敢开测,
            // 就得等佩戴者重进 app 才会开始。离屏自动取消。
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if !WorkoutMeasurer.shared.measuring {
                    await Scheduler.checkCommand(foreground: true)
                }
            }
        }
    }
}
