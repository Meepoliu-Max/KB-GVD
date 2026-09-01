"""服务维护任务（启动时执行）。

- apply_path_overrides：应用后台改动的存储路径（重启生效语义的实现点）
- purge_expired_files：按保留天数清理过期上传文件与结果目录

无后台调度器，均挂在 FastAPI lifespan 启动钩子上执行。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def apply_path_overrides(settings_store, app_settings) -> None:
    """应用系统设置中的存储路径覆盖（upload_dir / output_dir）。

    直接改 get_settings() 单例字段：后续请求经 Depends(get_settings)
    拿到的是同一对象，即刻对本次进程生效。
    """
    for key, attr in (("upload_dir", "upload_dir"), ("output_dir", "output_dir")):
        val = settings_store.get(key)
        if val and str(val) != getattr(app_settings, attr):
            try:
                Path(str(val)).mkdir(parents=True, exist_ok=True)
                setattr(app_settings, attr, str(val))
                logger.info("存储路径覆盖生效: %s -> %s", key, val)
            except OSError as e:
                logger.error("存储路径覆盖失败（沿用原路径）: %s=%s, %s", key, val, e)


def purge_expired_files(settings_store, task_store, app_settings, *, now: float | None = None) -> dict[str, int]:
    """清理过期文件（按保留天数，目录 mtime 判断）。

    - 上传原始文件超过 retain_file_days → 删除文件
    - 结果目录超过 retain_result_days → 删除目录并清理任务记录

    Returns:
        {"uploads": n, "outputs": m} 清理计数
    """
    now = now if now is not None else time.time()
    file_days = int(settings_store.get("retain_file_days") or 0)
    result_days = int(settings_store.get("retain_result_days") or 0)
    counts = {"uploads": 0, "outputs": 0}

    if file_days > 0:
        cutoff = now - file_days * 86400
        upload_dir = Path(app_settings.upload_dir)
        if upload_dir.exists():
            for f in upload_dir.iterdir():
                if not f.is_file() or f.name.startswith("."):
                    continue
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        counts["uploads"] += 1
                except OSError:
                    continue

    if result_days > 0:
        cutoff = now - result_days * 86400
        output_dir = Path(app_settings.output_dir)
        if output_dir.exists():
            for d in output_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    if d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                        task_store.delete(d.name)
                        counts["outputs"] += 1
                except OSError:
                    continue

    if counts["uploads"] or counts["outputs"]:
        logger.info(
            "过期清理：删除上传文件 %d 个，结果目录 %d 个", counts["uploads"], counts["outputs"]
        )
    return counts
