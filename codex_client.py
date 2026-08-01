from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

JsonObject = dict[str, Any] # e.g. (message: dict[str, Any]) == (message: JsonObject)

INITIALIZE_REQUEST_ID = 1
RATE_LIMITS_REQUEST_ID = 2


class CodexClientError(RuntimeError):
    """Codex App Server 통신 중 발생한 오류."""



@dataclass(frozen=True, slots=True) # dataclass 사용시 생성자 생략 가능, frozen=True(객체 변경X), slots=True(객체 허용된 속성만)
class UsageWindow:
    """하나의 Codex 사용량 제한 구간."""

    source: str
    used_percent: float
    duration_mins: int | None # int일 수도, None일 수도
    resets_at: datetime | None

    # method를 변수처럼 접근하게 만드는 decorator (e.g. usage.remaining_percent()=> usage.remaining_percent)
    @property  # 메서드를 괄호 없이 속성처럼 읽게 해주며, setter가 없으면 읽기 전용이 된다.
    def remaining_percent(self) -> float:
        """남은 비율을 계산한다."""
        return max(0.0, 100.0 - self.used_percent)

    @property
    def label(self) -> str:
        """시간 구간을 사람이 읽기 쉬운 이름으로 반환한다."""
        if self.duration_mins == 300:
            return "5시간 한도"
        if self.duration_mins == 10_080:
            return "주간 한도"
        if self.duration_mins is None:
            return "사용량 한도"
        return f"{self.duration_mins}분 한도"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """한 번 조회한 Codex 사용량 정보."""

    primary: UsageWindow | None
    secondary: UsageWindow | None
    fetched_at: datetime

    @property
    def windows(self) -> tuple[UsageWindow, ...]:
        """존재하는 한도만 모아서 반환"""
        return tuple(
            window
            for window in (self.primary, self.secondary)
            if window is not None
        )


# 앞에 _를 붙여 condex_client.py 내부용이라는 것을 명시
# 외부에서는 get_usage()만 호출
def _send_message(
    process: subprocess.Popen[str],
    message: JsonObject,
) -> None:
    """Codex App Server에 JSONL 메시지 한 줄을 전송한다."""
    if process.stdin is None:
        raise CodexClientError("App Server의 표준 입력을 열 수 없습니다.")

    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[str],
    expected_id: int,
) -> JsonObject:
    """지정한 요청 ID에 대한 응답이 올 때까지 메시지를 읽는다."""
    if process.stdout is None:
        raise CodexClientError("App Server의 표준 출력을 열 수 없습니다.")

    while True:
        line = process.stdout.readline()

        if not line:
            detail = ""
            if process.stderr is not None:
                detail = process.stderr.read().strip()

            message = "App Server가 응답 전에 종료되었습니다."
            if detail:
                message += f" 상세: {detail}"
            raise CodexClientError(message)

        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(response, dict) and response.get("id") == expected_id:
            return response


def _request(
    process: subprocess.Popen[str],
    *,
    method: str,
    request_id: int,
    params: JsonObject | None = None,
) -> JsonObject:
    """App Server에 요청을 보내고 result 객체를 반환한다."""
    message: JsonObject = {
        "method": method,
        "id": request_id,
    }
    if params is not None:
        message["params"] = params

    _send_message(process, message)
    response = _read_response(process, expected_id=request_id)

    if "error" in response:
        raise CodexClientError(f"{method} 오류: {response['error']}")

    result = response.get("result", {})
    if not isinstance(result, dict):
        raise CodexClientError(f"{method} 응답 형식이 올바르지 않습니다.")

    return result


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    timestamp = _to_int(value)
    if timestamp is None:
        return None

    try:
        return datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_window(source: str, data: Any) -> UsageWindow | None:
    """App Server의 한도 데이터를 UsageWindow로 변환한다."""
    if not isinstance(data, dict):
        return None

    used_percent = _to_float(data.get("usedPercent"))
    used_percent = min(100.0, max(0.0, used_percent))

    return UsageWindow(
        source=source,
        used_percent=used_percent,
        duration_mins=_to_int(data.get("windowDurationMins")),
        resets_at=_to_datetime(data.get("resetsAt")),
    )


def _start_app_server() -> subprocess.Popen[str]:
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW

    try:
        return subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
    except FileNotFoundError as error:
        raise CodexClientError(
            "codex 명령을 찾을 수 없습니다. Codex CLI 설치와 PATH를 확인하세요."
        ) from error


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def get_usage() -> UsageSnapshot:
    """Codex 사용량을 조회해 화면 코드가 사용할 데이터로 반환한다."""
    process = _start_app_server() # App Server 프로세스 실행 후 process에 저장

    try:
        # App Server에 클라이언트 연결 초기화
        _request(
            process,
            method="initialize",
            request_id=INITIALIZE_REQUEST_ID,
            params={
                "clientInfo": {
                    "name": "codex_usage_tray",
                    "title": "Codex Usage Tray",
                    "version": "0.1.0",
                }
            },
        )

        # 초기화 완료 알림
        _send_message(
            process,
            {
                "method": "initialized",
                "params": {},
            },
        )

        # Codex 사용량 정보 요청
        result = _request(
            process,
            method="account/rateLimits/read",
            request_id=RATE_LIMITS_REQUEST_ID,
        )

        # result 안에서 rateLimits 꺼내기
        rate_limits = result.get("rateLimits", {})
        if not isinstance(rate_limits, dict):
            raise CodexClientError("rateLimits 응답 형식이 올바르지 않습니다.")

        return UsageSnapshot(
            primary=_parse_window("primary", rate_limits.get("primary")),
            secondary=_parse_window("secondary", rate_limits.get("secondary")),
            fetched_at=datetime.now(),
        )
    finally: # 어떤 경우에도 App Server 종료
        _stop_process(process)


if __name__ == "__main__":
    try:
        snapshot = get_usage()

        if not snapshot.windows:
            print("사용량 한도 정보가 없습니다.")

        for window in snapshot.windows:
            reset_text = (
                window.resets_at.strftime("%Y-%m-%d %H:%M:%S")
                if window.resets_at is not None
                else "정보 없음"
            )
            print(f"\n[{window.label}]")
            print(f"사용함: {window.used_percent:g}%")
            print(f"남음: {window.remaining_percent:g}%")
            print(f"초기화: {reset_text}\n")
    except CodexClientError as error:
        print(f"오류: {error}", file=sys.stderr)
        raise SystemExit(1) from error
