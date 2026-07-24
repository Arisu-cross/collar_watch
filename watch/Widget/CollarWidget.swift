import WidgetKit
import SwiftUI

// 表盘小组件。内容是静态的——它的真正职责是让系统把 app 当作
// "表盘常驻",后台刷新预算升到最高档(一刻钟一档)。
// 想显示动态数据需要 App Group,以后要了再加。

struct CollarEntry: TimelineEntry { let date: Date }

struct CollarProvider: TimelineProvider {
    func placeholder(in context: Context) -> CollarEntry { CollarEntry(date: .now) }
    func getSnapshot(in context: Context, completion: @escaping (CollarEntry) -> Void) {
        completion(CollarEntry(date: .now))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<CollarEntry>) -> Void) {
        completion(Timeline(entries: [CollarEntry(date: .now)], policy: .never))
    }
}

// 表盘那格:一个爱心,撑满。SF Symbol,一定显示。
struct CollarComplicationView: View {
    var entry: CollarEntry
    var body: some View {
        Image(systemName: "heart.fill")
            .resizable()
            .scaledToFit()
            .padding(2)
            .widgetAccentable()
            .containerBackground(for: .widget) { Color.clear }
    }
}

@main
struct CollarWidgetBundle: WidgetBundle {
    var body: some Widget { CollarComplication() }
}

struct CollarComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "CollarComplication", provider: CollarProvider()) { entry in
            CollarComplicationView(entry: entry)
        }
        .configurationDisplayName("Collar")
        .description("让 Collar 保持后台上报。")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}
