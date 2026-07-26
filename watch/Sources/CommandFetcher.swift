import Foundation
import UserNotifications

// 按需测量指令通道。GET /command 拉服务端指令;POST /command/result 回执。
// 铁律:无指令时零动作——平时打开 app 就是普通上传界面。
enum CommandFetcher {
    struct Command: Decodable {
        let command: String?
        let command_id: String?
        let requested_at: String?
        let duration_seconds: Int?   // 测量时长由服务端下发,调时长不用重装 app
    }

    // 本地记账:执行过的指令永不重测(回执若迟到,服务端仍显示"已见",
    // 没有这道闸,重进 app 会把同一条指令再测一遍)
    private static let completedKey = "hrcmd.lastCompletedCommandID"
    static var lastCompletedID: String {
        UserDefaults.standard.string(forKey: completedKey) ?? ""
    }
    static func markCompleted(_ id: String) {
        UserDefaults.standard.set(id, forKey: completedKey)
    }

    static func fetch() async -> Command? {
        var req = URLRequest(url: Config.endpoint.appendingPathComponent("command"))
        req.httpMethod = "GET"
        req.setValue(Config.token, forHTTPHeaderField: "X-Health-Token")
        req.timeoutInterval = 10
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let cmd = try? JSONDecoder().decode(Command.self, from: data),
              cmd.command != nil else { return nil }
        if let cid = cmd.command_id, cid == lastCompletedID { return nil }
        return cmd
    }

    static func postResult(commandID: String, result: [String: Any]) async {
        var req = URLRequest(url: Config.endpoint.appendingPathComponent("command/result"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Config.token, forHTTPHeaderField: "X-Health-Token")
        req.timeoutInterval = 15
        let body: [String: Any] = ["command_id": commandID, "result": result]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }

    // 后台醒来撞见指令:发本地通知。点开通知=直接进 app,
    // scenePhase 变 active 后自动取指令开测。
    static func notifyPickup() {
        let content = UNMutableNotificationContent()
        content.title = "Collar"
        content.body = "收到测量请求,点开 Collar 开始。"
        content.sound = .default
        let req = UNNotificationRequest(identifier: "hrcmd-pickup",
                                        content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }
}
