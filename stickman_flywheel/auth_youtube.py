"""YouTube 채널 1회 원터치 연동 및 인증 도구 (PKCE 에러 방지 & 안정 핸들러)."""
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

OAUTH_PATH = CONFIG_DIR / "oauth_client.json"
TOKEN_PATH = CONFIG_DIR / "youtube_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

auth_code = None
server_error = None


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code, server_error
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>인증 완료</title></head>
            <body style="font-family: sans-serif; text-align: center; padding-top: 60px; background: #0F172A; color: #FFFFFF;">
                <h1 style="color: #10B981; font-size: 2rem;">🎉 유튜브 채널 연동이 완료되었습니다!</h1>
                <p style="font-size: 1.15rem; color: #94A3B8; margin-top: 16px;">
                    이제 이 브라우저 창을 닫으셔도 됩니다.
                </p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        elif "error" in params:
            server_error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"인증 오류: {server_error}".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def authenticate_channel():
    print("=" * 60)
    print("[YouTube Official Channel Connection Tool]")
    print("스틱맨 숏폼 플라이휠 - YouTube 공식 채널 연동 도구")
    print("=" * 60)

    if not OAUTH_PATH.exists():
        print(f"\n[!] OAuth 클라이언트 파일이 없습니다: {OAUTH_PATH}")
        return False

    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = None
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print(">> 기존 토큰 만료됨 -> 자동 갱신 시도 중...")
                creds.refresh(Request())
            else:
                flow = Flow.from_client_secrets_file(
                    str(OAUTH_PATH),
                    scopes=SCOPES,
                    redirect_uri="http://localhost:8080/",
                )
                # PKCE 비활성화 (Missing code verifier 원천 차단)
                flow.autogenerate_code_verifier = False
                auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")

                url_file = BASE_DIR.parent / "scratch" / "auth_url.txt"
                url_file.parent.mkdir(parents=True, exist_ok=True)
                url_file.write_text(auth_url, encoding="utf-8")

                print("\n" + "=" * 60)
                print("[!] 아래 유튜브 인증 링크를 클릭하여 브라우저에서 열어주세요:")
                print(auth_url)
                print("=" * 60 + "\n")

                if sys.platform == "win32":
                    try:
                        import webbrowser
                        webbrowser.open(auth_url)
                    except Exception:
                        pass

                httpd = HTTPServer(("localhost", 8080), OAuthCallbackHandler)
                print(">> 인증 응답 대기 중 (http://localhost:8080/)...")
                httpd.handle_request()
                httpd.server_close()

                if not auth_code:
                    print(f"\n[오류] 인증 코드를 받지 못했습니다: {server_error}")
                    return False

                print(">> 인증 코드 수신 완료! 토큰 교환 중...")
                flow.fetch_token(code=auth_code)
                creds = flow.credentials

            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            print(f">> 인증 토큰이 안전하게 저장되었습니다: {TOKEN_PATH.name}")

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        res = youtube.channels().list(part="snippet,statistics", mine=True).execute()

        items = res.get("items", [])
        if not items:
            print(">> 로그인한 계정에 생성된 유튜브 채널이 없습니다.")
            return False

        ch = items[0]
        title = ch["snippet"]["title"]
        ch_id = ch["id"]
        subs = ch["statistics"].get("subscriberCount", "0")
        video_cnt = ch["statistics"].get("videoCount", "0")

        print("\n" + "=" * 60)
        print("[축하합니다!] 유튜브 채널이 우리 앱/프로그램에 성공적으로 연결되었습니다!")
        print(f">> 연결된 채널명: {title}")
        print(f">> 채널 고유 ID : {ch_id}")
        print(f">> 총 동영상 수 : {video_cnt}개 | 구독자 수: {subs}명")
        print("=" * 60)
        print("이제 앱에서 생성된 쇼츠 영상이 이 채널로 100% 자동 업로드됩니다.\n")
        return True

    except Exception as e:
        print(f"\n[오류 발생] 인증 진행 중 오류: {e}")
        return False


if __name__ == "__main__":
    authenticate_channel()
