import XCTest
@testable import CounterDemo

final class CounterTests: XCTestCase {
    func testIncrement() { XCTAssertEqual(Counter.increment(0), 1) }
}
