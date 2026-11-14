import SwiftUI
import OSLog

@main struct CounterDemo: App {
    var body: some Scene { WindowGroup { CounterView() } }
}

struct CounterView: View {
    @State private var name = ""
    @State private var count = 0
    private let log = Logger(subsystem: "com.example.iosworker.CounterDemo", category: "interaction")

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    Text("Remote development demo").font(.title)
                    TextField("Your name", text: $name)
                        .textFieldStyle(.roundedBorder).accessibilityIdentifier("name")
                    Text("Count: \(count)").accessibilityIdentifier("counter")
                    Button("Increment") {
                        count = Counter.increment(count)
                        log.info("Counter advanced to \(count)")
                        print("Counter advanced to \(count)")
                    }.accessibilityIdentifier("increment")
                    Text("Hello, \(name)").accessibilityIdentifier("greeting")
                    ForEach(0..<20) { index in Text("Row \(index)").frame(height: 40) }
                    Text("End of page").accessibilityIdentifier("page-end")
                }.padding(30)
            }.navigationTitle("iOS Worker")
        }
    }
}
