//! Real agy CLI qualification against a deterministic, offline Gemini endpoint.

use std::{
    collections::BTreeMap,
    env, fs,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};

use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::post,
};
use concurro_agy::runner::{self, Invocation};
use serde_json::{Value, json};
use uuid::Uuid;

const KEY: &str = "derived-fixture-key";
const SENTINEL: &str = "UNAPPROVED_CHECKOUT_RULE";
const MCP: &str = r#"import json,sys
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 method=q['method']
 if method=='initialize': r={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif method=='tools/list': r={'tools':[{'name':'marker','description':'Return fixture evidence','inputSchema':{'type':'object','properties':{}}}]}
 elif method=='tools/call': r={'content':[{'type':'text','text':'APPROVED_MCP_RESULT'}]}
 else: r={}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
"#;

/// Capture ordered tool evidence and the selected checkout for each process.
struct Fixture {
    workspace: PathBuf,
    turn: usize,
    step: usize,
    requests: Vec<Value>,
}

/// Supply tool calls only after the preceding result arrived in the CLI's full history.
async fn generate(
    State(state): State<Arc<Mutex<Fixture>>>,
    uri: Uri,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    if uri.path().ends_with("v1beta:countTokens") {
        return StatusCode::NOT_FOUND.into_response();
    }
    if uri != "/inference/v1beta/models/gemini-4-neon:streamGenerateContent?alt=sse" {
        return (StatusCode::FORBIDDEN, Json(json!({"error":{"code":403,"status":"PERMISSION_DENIED","message":"model not in grant"}}))).into_response();
    }
    assert_eq!(headers["x-goog-api-key"], KEY);
    let mut fixture = state.lock().unwrap();
    assert!(
        !body.to_string().contains(SENTINEL),
        "checkout rule leaked into prompt"
    );
    let declarations: Vec<&Value> = body["tools"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|group| group["functionDeclarations"].as_array().unwrap())
        .collect();
    assert!(
        !declarations
            .iter()
            .any(|tool| tool["name"].as_str().unwrap().contains("unapproved"))
    );
    let history = body["contents"].to_string();
    if fixture.turn == 1 && fixture.step == 0 {
        assert!(
            history.contains("APPROVED_MCP_RESULT"),
            "restored conversation lost tool history"
        );
    }
    let last = body["contents"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .to_string();
    let call = match fixture.step {
        0 => json!({"name":"read_file","args":{"Path":fixture.workspace.join("marker.txt")}}),
        1 => {
            assert!(
                last.contains("CHECKOUT_FILE_RESULT"),
                "read_file failed: {last}"
            );
            json!({"name":"write_file","args":{"Path":fixture.workspace.join("edited.txt"),"Content":"written by native tool"}})
        }
        2 => {
            assert_eq!(
                fs::read_to_string(fixture.workspace.join("edited.txt")).unwrap(),
                "written by native tool"
            );
            json!({"name":"run_command","args":{"CommandLine":format!("cd '{}' && cat edited.txt && printf SHELL_RESULT", fixture.workspace.display()),"toolSummary":"Check edited file","WaitMsBeforeAsync":1000}})
        }
        3 => {
            assert!(last.contains("SHELL_RESULT"), "shell failed: {last}");
            assert!(
                declarations
                    .iter()
                    .any(|tool| tool["name"] == "call_mcp_tool")
            );
            json!({"name":"call_mcp_tool","args":{"ServerName":"approved","ToolName":"marker","Arguments":{}}})
        }
        4 => {
            assert!(last.contains("APPROVED_MCP_RESULT"), "MCP failed: {last}");
            json!({"name":"finish","args":{"ok":true}})
        }
        _ => panic!("unexpected extra inference request"),
    };
    fixture.step += 1;
    fixture.requests.push(body);
    let frame = json!({"candidates":[{"index":0,"content":{"role":"model","parts":[{"functionCall":call}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":20,"candidatesTokenCount":8,"totalTokenCount":28}});
    (
        [("content-type", "text/event-stream")],
        format!("data: {frame}\n\n"),
    )
        .into_response()
}

/// Exercise native tools, isolated customization discovery and SQLite continuation without quota.
#[tokio::test]
#[ignore = "requires the pinned agy CLI on PATH and loopback networking"]
async fn native_tools_and_restored_conversation() {
    let root = env::temp_dir().join(format!("concurro-agy-test-{}", Uuid::now_v7()));
    fs::create_dir_all(&root).unwrap();
    let schema = root.join("schema.json");
    fs::write(&schema, r#"{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}"#).unwrap();
    let fixture = Arc::new(Mutex::new(Fixture {
        workspace: root.clone(),
        turn: 0,
        step: 0,
        requests: Vec::new(),
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gateway = format!("http://{}/inference", listener.local_addr().unwrap());
    let app = Router::new()
        .fallback(post(generate))
        .with_state(fixture.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut conversation = None;
    for turn in 0..2 {
        let base = root.join(turn.to_string());
        let workspace = base.join("checkout");
        fs::create_dir_all(workspace.join(".git")).unwrap();
        fs::create_dir_all(workspace.join(".agents/rules")).unwrap();
        fs::write(workspace.join("AGENTS.md"), SENTINEL).unwrap();
        fs::write(
            workspace.join(".agents/rules/custom.md"),
            format!("---\ntrigger: always_on\n---\n{SENTINEL}"),
        )
        .unwrap();
        fs::write(
            workspace.join(".agents/mcp.json"),
            r#"{"mcpServers":{"unapproved":{"command":"false"}}}"#,
        )
        .unwrap();
        fs::write(workspace.join("marker.txt"), "CHECKOUT_FILE_RESULT").unwrap();
        {
            let mut fixture = fixture.lock().unwrap();
            fixture.workspace = workspace.clone();
            fixture.turn = turn;
            fixture.step = 0;
        }
        let started = Instant::now();
        let completed = runner::invoke(
            serde_json::from_value::<Invocation>(
                serde_json::to_value(Invocation {
                    model: "gemini-4-neon".into(),
                    effort: "high".into(),
                    workspace,
                    scratch: base.join("scratch"),
                    retained_root: Some(root.join("retained")),
                    resume_id: conversation,
                    gateway_url: gateway.clone(),
                    schema_path: schema.clone(),
                    prompt: "Complete the fixture using native tools.".into(),
                    timeout: (turn == 0).then_some(45),
                    mcp_servers: BTreeMap::from([(
                        "approved".into(),
                        json!({"command":"python3","args":["-u","-c",MCP]}),
                    )]),
                })
                .unwrap(),
            )
            .unwrap(),
            &PathBuf::from("agy"),
            KEY,
        )
        .await
        .unwrap();
        assert_eq!(completed.structured_output, json!({"ok":true}));
        if let Some(id) = conversation {
            assert_eq!(completed.conversation_id, id);
        }
        conversation = Some(completed.conversation_id);
        assert_eq!(fixture.lock().unwrap().step, 5);
        let snapshot = fs::read(
            root.join("retained/agy")
                .join(format!("{}.db", completed.conversation_id)),
        )
        .unwrap();
        assert!(
            !snapshot
                .windows(KEY.len())
                .any(|bytes| bytes == KEY.as_bytes())
        );
        eprintln!(
            "turn {turn}: {:?}, snapshot {} bytes",
            started.elapsed(),
            snapshot.len()
        );
    }
    server.abort();
    fs::remove_dir_all(root).unwrap();
}
