// applefm — JSON-lines bridge to Apple's on-device Foundation Models for mlxcli/mlxgui.
// stdin : {"op":"status"} | {"op":"reset","instructions":"..."} | {"op":"ask","prompt":"...","instructions":"..."}
// stdout: {"ok":true,"text":"..."} | {"ok":false,"error":"..."}
import Foundation
import FoundationModels

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

@main
struct Main {
    static func main() async {
        let model = SystemLanguageModel.default
        var instructions = ""
        var session: LanguageModelSession? = nil

        while let line = readLine() {
            guard let data = line.data(using: .utf8),
                  let req = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let op = req["op"] as? String else {
                emit(["ok": false, "error": "bad request"]); continue
            }
            if op == "status" {
                if case .available = model.availability { emit(["ok": true, "text": "available"]) }
                else { emit(["ok": false, "error": "unavailable: \(model.availability)"]) }
                continue
            }
            guard case .available = model.availability else {
                emit(["ok": false, "error": "unavailable: \(model.availability)"]); continue
            }
            if let ins = req["instructions"] as? String { instructions = ins }
            if op == "reset" || session == nil {
                session = LanguageModelSession(instructions: instructions)
                if op == "reset" { emit(["ok": true, "text": ""]); continue }
            }
            guard op == "ask", let prompt = req["prompt"] as? String else {
                emit(["ok": false, "error": "unknown op"]); continue
            }
            do {
                let r = try await session!.respond(to: prompt)
                emit(["ok": true, "text": r.content])
            } catch {
                // Context window exceeded or guardrail: start fresh so the next ask can work.
                session = LanguageModelSession(instructions: instructions)
                emit(["ok": false, "error": "\(error)"])
            }
        }
    }
}
