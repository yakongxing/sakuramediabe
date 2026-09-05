#!/usr/bin/env python3
"""容器健康检查：确认 uvicorn 正在监听并能完成一次 HTTP 往返。

基础镜像 python:3.10-slim-bookworm 不含 curl/wget，因此用标准库实现。

判定口径刻意保持"存活"而非"就绪"：
- 任何 HTTP 状态码（含 404/401）都说明 uvicorn 已接管连接并能返回响应，视为健康。
  仓库内无根路由，`GET /` 正常返回 404；`/status` 挂了 get_current_user 会返回 401。
  把这两者当失败会导致健康的容器被误判。
- 5xx 说明应用自身出错，视为不健康。
- 连接被拒绝/超时说明进程未起或已卡死，视为不健康。
"""

from __future__ import annotations

import http.client
import os
import sys

HOST = os.environ.get("SAKURAMEDIA_HEALTHCHECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("SAKURAMEDIA_HEALTHCHECK_PORT", "8000"))
PATH = os.environ.get("SAKURAMEDIA_HEALTHCHECK_PATH", "/")
TIMEOUT = float(os.environ.get("SAKURAMEDIA_HEALTHCHECK_TIMEOUT", "5"))


def main() -> int:
    connection = http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT)
    try:
        connection.request("GET", PATH)
        status = connection.getresponse().status
    except OSError as exc:
        print(f"unhealthy: cannot reach http://{HOST}:{PORT}{PATH} detail={exc}")
        return 1
    except http.client.HTTPException as exc:
        print(f"unhealthy: malformed HTTP response detail={exc}")
        return 1
    finally:
        connection.close()

    if status >= 500:
        print(f"unhealthy: server error status={status}")
        return 1
    print(f"healthy: status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
