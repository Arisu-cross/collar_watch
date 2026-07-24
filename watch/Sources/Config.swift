import Foundation

enum Config {
    static let endpoint = URL(string: "https://your-server.example.com/api/health")!
    // 测试期用 "filter_test"(服务端面板自动排除,不污染真数据);
    // 模拟器全链路验收通过后切 "watch" 再装真机。
    static let source = "watch"
    static var token: String { ConfigLocal.token }
}
