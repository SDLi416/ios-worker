import XCTest

final class CounterUITests: XCTestCase {
    func testIncrementButton() {
        let app = XCUIApplication()
        app.launch()
        app.buttons["increment"].tap()
        XCTAssertEqual(app.staticTexts["counter"].label, "Count: 1")
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
