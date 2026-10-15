"""covers: TC-011

TC-011 修改配置并重启后调度频率生效（US2-4）
"""
from __future__ import annotations

from app.runtime.scheduler.engine import SchedulerEngine
from app.runtime.scheduler.registry import cron_config_key, discover_jobs
from config.properties import RuntimeProperties


class TestJobRegistry:
    """TC-011: 频率全部走配置；改 JOB_CRON_* 重启（重新装配）即生效，无需改代码。"""

    def test_default_cron_from_job_declaration(self, monkeypatch):
        monkeypatch.delenv("JOB_CRON_DOC_POLL", raising=False)
        registry = discover_jobs()
        assert registry.cron_for("doc_poll") == "*/3 * * * *"  # 默认 3 分钟

    async def test_config_override_applies_on_reassembly(self, monkeypatch):
        monkeypatch.setenv("JOB_CRON_DOC_POLL", "*/1 * * * *")  # 管理员改为 1 分钟
        # 模拟重启：重新装配注册表与调度引擎
        registry = discover_jobs()
        assert registry.cron_for("doc_poll") == "*/1 * * * *"

        engine = SchedulerEngine("alpha")
        engine.register_jobs(registry)
        engine.start()
        try:
            trigger = engine.get_job("doc_poll").trigger
            assert "minute='*/1'" in str(trigger)  # 调度器按新频率触发
        finally:
            engine.shutdown()

    def test_runtime_properties_expose_cron_and_thresholds(self, monkeypatch):
        monkeypatch.setenv("JOB_CRON_TAPD_SYNC", "*/2 * * * *")
        props = RuntimeProperties()
        assert props.job_cron_tapd_sync == "*/2 * * * *"
        assert props.job_cron_doc_poll == "*/3 * * * *"
        assert props.misfire_grace_seconds == 300
        assert cron_config_key("doc_poll") == "JOB_CRON_DOC_POLL"

    def test_discover_registers_all_jobs(self):
        registry = discover_jobs(lease_keeper=object())
        names = {j.name for j in registry.all()}
        assert names == {
            "doc_poll",
            "tapd_sync",
            "repo_sync",
            "todo_readback",
            "index_refresh",
            "loop_digest",
            "lease_reaper",
        }
        # 派生型 Agentic Job 无 cron，不常驻调度
        scheduled = {j.name for j in registry.scheduled_jobs()}
        assert "index_refresh" not in scheduled
        assert "doc_poll" in scheduled
