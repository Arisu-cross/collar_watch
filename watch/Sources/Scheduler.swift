import Foundation
import WatchKit
import UserNotifications

// 后台刷新链。铁律:每次醒来先预约下一次再干活——采集/上传途中崩溃,
// 链条也不断。预约的 15 分钟是"最早不早于",实际间隔系统按预算给
// (带表盘小组件时最高一刻钟一档)。
enum Scheduler {
    static let interval: TimeInterval = 15 * 60

    static func scheduleNext() {
        WKApplication.shared().scheduleBackgroundRefresh(
            withPreferredDate: Date().addingTimeInterval(interval),
            userInfo: nil) { _ in }
    }

    // 指令检查(独立可调):runCycle 开头调,前台轻轮询也调。
    // 前台→直接开 workout session 实测;后台→只发本地通知
    // (watchOS 不允许后台启动 workout session,点通知进 app 即自动开测)。
    static func checkCommand(foreground: Bool) async {
        if let cmd = await CommandFetcher.fetch(),
           cmd.command == "measure_heart_rate",
           let cid = cmd.command_id {
            if foreground {
                let secs = cmd.duration_seconds ?? 30
                await MainActor.run {
                    WorkoutMeasurer.shared.start(commandID: cid, duration: secs)
                }
            } else {
                CommandFetcher.notifyPickup()
            }
        }
    }

    // 一个完整采集上报周期:点测类增量 + 睡眠全量聚合,合批发送。
    // 进门先查指令;无指令→零动作,照旧采集上报。
    static func runCycle(foreground: Bool = true) async {
        await checkCommand(foreground: foreground)
        let (samples, anchors) = await HealthCollector.shared.collect()
        let sleep = await SleepAggregator.collect(store: HealthCollector.shared.store)
        do {
            try Uploader.shared.send(samples: samples + sleep, pendingAnchors: anchors)
        } catch {
            Status.shared.note(failure: -1, message: error.localizedDescription)
        }
    }
}

final class ExtensionDelegate: NSObject, WKApplicationDelegate {
    func applicationDidFinishLaunching() {
        Scheduler.scheduleNext()   // 前台启动重建链,后台链断掉时的自愈入口
        // 本地通知授权(后台撞见指令时提醒佩戴者,点通知直接进 app)
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func handle(_ backgroundTasks: Set<WKRefreshBackgroundTask>) {
        for task in backgroundTasks {
            switch task {
            case let refresh as WKApplicationRefreshBackgroundTask:
                Scheduler.scheduleNext()
                Status.shared.noteBackgroundWake(Date())   // 真机验收定时链的观测点
                Task {
                    await Scheduler.runCycle(foreground: false)
                    refresh.setTaskCompletedWithSnapshot(false)
                }
            case let urlTask as WKURLSessionRefreshBackgroundTask:
                Uploader.shared.pendingSessionTask = {
                    urlTask.setTaskCompletedWithSnapshot(false)
                }
                Uploader.shared.reconnectBackgroundSession()
            default:
                task.setTaskCompletedWithSnapshot(false)
            }
        }
    }
}
