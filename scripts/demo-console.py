"""Start a loopback dashboard with synthetic data and sending disabled."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from wechat_agent.config import Settings
from wechat_agent.desktop import DesktopController, DesktopRequestHandler, DesktopServer
from wechat_agent.memory_orchestrator import MemoryOrchestrator
from wechat_agent.memory_runtime import MemoryRuntime
from wechat_agent.models import MessageEvent
from wechat_agent.proactive_scheduler import ProactiveScheduler
from wechat_agent.semantic_profiles import SemanticProfiles
from wechat_agent.storage import Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    root = Path("state").resolve()
    root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="console-demo-", dir=root))
    folder.chmod(0o700)
    db_url = "sqlite:///" + (folder / "memory.db").as_posix()
    config = folder / ".env"
    config.write_text("\n".join([
        "WECHAT_PLATFORM=linux", "WECHAT_ACCOUNT_ID=demo-bot", "WECHAT_GROUP_NAMES=演示测试群,空群演示",
        'WECHAT_LINUX_CHAT_IDS=\'{"演示测试群":"demo-group@chatroom","空群演示":"demo-empty@chatroom"}\'',
        "WECHAT_ALLOW_SEND=false", "WECHAT_PROACTIVE_REPLY=false", "PROACTIVE_SCHEDULE_ENABLED=false",
        "MEMORY_ORCHESTRATOR_ENABLED=true", "MEMORY_EVENT_EXTRACTION_ENABLED=true",
        "SEMANTIC_GROUP_PROFILE_ENABLED=true", "EMOTION_LAYER_ENABLED=true", "EMBEDDING_ENABLED=false", "PROFILE_SYNTHESIS_ENABLED=false",
        "PLATFORM_BROWSER_ENABLED=false", "PLATFORM_WRITE_ENABLED=false", "LLM_API_KEY=", "VISION_API_KEY=", "DESKTOP_TOKEN=", "WECHAT_AI_ALIASES=小鱼,AI", "PERSONA_NAME=小鱼",
        "DATABASE_URL=" + db_url, "WECHAT_STOP_FILE=" + (folder / "STOP").as_posix(),
    ]) + "\n", encoding="utf-8")
    config.chmod(0o600)
    settings = Settings(_env_file=config, database_url=db_url, wechat_allow_send=False, embedding_enabled=False, llm_api_key="", vision_api_key="", platform_write_enabled=False)
    storage = Storage(settings)
    storage.init_schema()
    now = datetime.now(timezone.utc)
    runtime = MemoryRuntime(storage)
    for day in (3, 2, 1):
        for index in range(8):
            member = "demo-A" if index % 2 == 0 else "demo-B"
            source = MessageEvent("demo-bot", "demo-group@chatroom", "演示测试群", member, "小林" if index % 2 == 0 else "小周", f"Python 代码和数据库接口设计讨论第 {day} 天", observed_at=now - timedelta(days=day, minutes=-index), external_message_id=f"demo-topic-{day}-{index}", historical=True)
            storage.record_message(source)
    for identifier, member, name, text in (
        ("demo-insult", "demo-A", "小林", "小鱼你就是废物"),
        ("demo-comfort", "demo-B", "小周", "小鱼别难过，辛苦了"),
        ("demo-question", "demo-C", "小陈", "刚才谁骂你了"),
    ):
        source = MessageEvent("demo-bot", "demo-group@chatroom", "演示测试群", member, name, text, observed_at=now, external_message_id=identifier)
        storage.record_message(source)
    while runtime.process_analysis():
        pass
    SemanticProfiles(runtime).refresh("demo-bot", "demo-group@chatroom", force=True)
    MemoryOrchestrator(runtime).retrieve_evidence(source)
    ProactiveScheduler(runtime).heartbeat("demo-bot", "demo-empty@chatroom")
    controller = DesktopController(settings, config_path=config)
    server = DesktopServer(("127.0.0.1", args.port), DesktopRequestHandler, controller, "")
    print(f"Synthetic console: http://127.0.0.1:{args.port} ; sending=false ; config={config}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        storage.engine.dispose()
        controller._storage.engine.dispose()


if __name__ == "__main__":
    main()
