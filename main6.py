import os
import sys
import boto3
import re
import json
import subprocess
import time
import docker
import threading
import http.client
import urllib.error
import urllib.request
import signal
import argparse
import sqlite3
import socket # 프록시 헬스 체크를 위해 추가
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import NoCredentialsError, ClientError
from botocore.client import Config
from pytubefix import YouTube, Playlist, Channel
from pytubefix.exceptions import BotDetection, PytubeFixError
from datetime import datetime

# --- 설정 (사용자 환경에 맞게 ★★★반드시★★★ 수정) ---

# -- Backblaze B2 정보 --
B2_KEY_ID = 'AKIAZHLGGPDCABBPDRBDCALDKF3A2D3M74DGUCNJ4QZN'
B2_APPLICATION_KEY = 'uFVQTm2IU9Qq1j3QgAyzkz7UKHyTlDB2Piuaaarg'
B2_ENDPOINT_URL = 'https://s3.eu-central-1.s4.mega.io'
B2_BUCKET_NAME = 'archive'

# -- 로컬 다운로드 경로 정보 --
DOWNLOAD_BASE_DIR = r'./temp'
DB_PATH = os.path.join(DOWNLOAD_BASE_DIR, 'download_log.sqlite') # SQLite DB 경로

# -- 프로세스 제어 설정 --
MAX_WORKERS = 10
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
RESTART_WAIT_SECONDS = 30
MAX_DOWNLOAD_RETRIES = 4
DOWNLOAD_RETRY_DELAY_SECONDS = 5
PREFERRED_RESOLUTIONS = ['720p', '480p', '360p'] # 선호하는 해상도 순서

# [수정] 프록시 재시작 쿨다운 시간(초) 추가
PROXY_RESTART_COOLDOWN = 90

# -- 프록시 및 도커 설정 (선택 사항) --
DOCKER_CONTAINER_NAME_TO_RESTART = 'docker-warproxy3'
USE_PROXY = True
PROXY_PROTOCOL = "socks5"
PROXY_HOST_PORT = "127.0.0.1:8002" # 프록시 서버의 IP와 포트. 이 스크립트가 실행되는 머신에서 접근 가능해야 합니다.
USE_PROXY_AUTH = True
PROXY_USERNAME = "test"
PROXY_PASSWORD = "test"


# --- 안전한 종료 및 병렬 처리 관리를 위한 전역 객체 ---

class GracefulShutdown:
    """
    종료 신호(SIGINT, SIGTERM)를 감지하고 모든 스레드에 종료 상태를 전파하여
    안전하게 작업을 마무리할 수 있도록 돕는 클래스입니다.
    """
    def __init__(self):
        self.shutdown_event = threading.Event()
        signal.signal(signal.SIGINT, self.handler)
        signal.signal(signal.SIGTERM, self.handler)

    def handler(self, signum, frame):
        print(f"\n🚨 종료 신호({signal.Signals(signum).name}) 수신! 새 작업을 중단하고 현재 작업을 안전하게 마무리합니다...")
        self.shutdown_event.set()

    def is_shutting_down(self):
        return self.shutdown_event.is_set()

class ProxyManager:
    """병렬 처리 환경에서 프록시 상태를 안전하게 관리합니다."""
    def __init__(self):
        self._proxy_ok_event = threading.Event()
        self._proxy_ok_event.set() # 초기에는 프록시가 정상이라고 가정
        self._restart_lock = threading.Lock()
        self._last_restart_time = 0 # [추가] 마지막 재시작 시간 기록

    def wait_for_proxy(self):
        """프록시가 재시작 중이면 대기합니다."""
        self._proxy_ok_event.wait()

    def handle_restart(self, reason="BOT 감지"):
        """
        프록시 재시작을 처리합니다. 여러 스레드에서 동시에 호출될 경우
        한 번만 재시작이 이루어지도록 Lock을 사용하며, 쿨다운을 적용합니다.
        """
        with self._restart_lock:
            # [수정] 재시작 쿨다운 확인
            now = time.time()
            if now - self._last_restart_time < PROXY_RESTART_COOLDOWN:
                print(f"🕒 프록시 재시작 쿨다운 중... ({int(PROXY_RESTART_COOLDOWN - (now - self._last_restart_time))}초 남음)")
                self._proxy_ok_event.wait() # 다른 스레드가 시작한 재시작이 끝날 때까지 대기
                return

            # 이미 다른 스레드에 의해 재시작이 진행 중이면 추가 실행 방지
            if not self._proxy_ok_event.is_set():
                self._proxy_ok_event.wait()
                return

            print(f"🚨🚨🚨 {reason}! 모든 작업을 일시 중지하고 프록시 재시작을 시작합니다. 🚨🚨🚨")
            self._proxy_ok_event.clear() # 프록시가 재시작 중임을 표시
            self._last_restart_time = time.time() # 재시작 시간 기록

        try:
            restart_container(DOCKER_CONTAINER_NAME_TO_RESTART)
            print(f"🕒 프록시 안정화를 위해 {RESTART_WAIT_SECONDS}초 대기합니다...")
            time.sleep(RESTART_WAIT_SECONDS)
        finally:
            print("✅ 프록시 재시작 완료! 모든 작업을 재개합니다.")
            self._proxy_ok_event.set() # 프록시가 다시 정상임을 표시

# --- SQLite 데이터베이스 함수 ---

def init_database(db_path):
    """데이터베이스와 테이블을 초기화합니다."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # 완료된 작업 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS completed_tasks (
                video_id TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('video', 'thumbnail')),
                title TEXT,
                author TEXT,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (video_id, task_type)
            )
        ''')
        # 오류 로그 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT,
                url TEXT,
                title TEXT,
                author TEXT,
                error_message TEXT,
                logged_at TEXT NOT NULL
            )
        ''')
        # 메타데이터 캐시 테이블 추가
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS video_metadata_cache (
                video_id TEXT PRIMARY KEY NOT NULL,
                title TEXT,
                author TEXT,
                url TEXT,
                cached_at TEXT NOT NULL
            )
        ''')
        conn.commit()
    print(f"✅ 데이터베이스 초기화 완료: {db_path}")

def get_completed_ids(db_path):
    """데이터베이스에서 완료된 비디오 및 썸네일 ID 세트를 가져옵니다."""
    completed_video_ids = set()
    completed_thumbnail_ids = set()
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT video_id FROM completed_tasks WHERE task_type = 'video'")
            completed_video_ids.update(row[0] for row in cursor.fetchall())
            cursor.execute("SELECT video_id FROM completed_tasks WHERE task_type = 'thumbnail'")
            completed_thumbnail_ids.update(row[0] for row in cursor.fetchall())
    except sqlite3.Error as e:
        print(f"❌ 데이터베이스에서 완료 목록을 읽는 중 오류 발생: {e}")
    return completed_video_ids, completed_thumbnail_ids

def log_completion(db_path, video_id, task_type, title, author):
    """완료된 작업을 데이터베이스에 기록합니다."""
    thread_id = threading.current_thread().name
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO completed_tasks (video_id, task_type, title, author, completed_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (video_id, task_type, title, author, datetime.now().isoformat()))
            conn.commit()
            print(f"✅ [{thread_id}] DB 로그 저장 성공: {task_type} -> {title}")
    except sqlite3.Error as e:
        print(f"❌ [{thread_id}] DB 로그 저장 실패: {e}")

def log_error(db_path, video_id, url, title, author, error_message):
    """발생한 오류를 데이터베이스에 기록합니다."""
    thread_id = threading.current_thread().name
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO error_logs (video_id, url, title, author, error_message, logged_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (video_id, url, title, author, str(error_message), datetime.now().isoformat()))
            conn.commit()
    except sqlite3.Error as e:
        print(f"❌ [{thread_id}] DB 오류 로그 저장 실패: {e}")

def get_metadata_from_cache(db_path, video_id):
    """SQLite 캐시에서 비디오 메타데이터를 가져옵니다."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT title, author, url FROM video_metadata_cache WHERE video_id = ?", (video_id,))
            row = cursor.fetchone()
            if row:
                print(f"✅ 캐시에서 메타데이터 로드: {video_id}")
                return {
                    "id": video_id,
                    "title": row[0],
                    "author": row[1],
                    "url": row[2]
                }
    except sqlite3.Error as e:
        print(f"❌ 캐시에서 메타데이터를 읽는 중 오류 발생: {e}")
    return None

def save_metadata_to_cache(db_path, video_meta):
    """비디오 메타데이터를 SQLite 캐시에 저장합니다."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO video_metadata_cache (video_id, title, author, url, cached_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (video_meta['id'], video_meta['title'], video_meta['author'], video_meta['url'], datetime.now().isoformat()))
            conn.commit()
            print(f"✅ 메타데이터 캐시 저장 성공: {video_meta['id']}")
    except sqlite3.Error as e:
        print(f"❌ 메타데이터 캐시 저장 실패: {e}")


# --- 로직 함수 ---

def check_proxy_health(proxy_host_port, timeout=5):
    """
    프록시 서버의 가용성을 확인합니다.
    SOCKS5 프록시의 경우 직접적인 HTTP 요청을 통한 확인이 복잡하므로,
    기본적으로는 프록시 주소의 포트가 열려있는지 확인합니다.
    """
    host, port_str = proxy_host_port.split(':')
    try:
        port = int(port_str)
    except ValueError:
        print(f"❌ 프록시 포트가 유효하지 않습니다: {port_str}")
        return False

    try:
        # 소켓을 사용하여 프록시 서버에 연결 시도
        with socket.create_connection((host, port), timeout=timeout) as sock:
            print(f"✅ 프록시 서버 {host}:{port}에 연결 성공.")
            return True
    except ConnectionRefusedError:
        print(f"❌ 프록시 서버 {host}:{port}가 연결을 거부했습니다. 서버가 실행 중이거나 올바른 포트에서 수신 대기 중인지 확인하세요.")
        return False
    except socket.timeout:
        print(f"❌ 프록시 서버 {host}:{port} 연결 시간 초과. 네트워크 문제 또는 서버 응답 없음.")
        return False
    except Exception as e:
        print(f"❌ 프록시 서버 {host}:{port} 연결 중 알 수 없는 오류 발생: {e}")
        return False

def restart_container(container_name_or_id):
    """Docker 컨테이너를 재시작합니다."""
    if not container_name_or_id:
        print("⚠️ Docker 컨테이너 이름이 지정되지 않아 재시작을 건너뜁니다.")
        return False
    try:
        client = docker.from_env()
        container = client.containers.get(container_name_or_id)
        print(f"🔄 '{container.name}' 컨테이너를 재시작합니다...")
        container.restart()
        print(f"✅ '{container.name}' 컨테이너가 성공적으로 재시작되었습니다.")
        return True
    except (docker.errors.NotFound, docker.errors.DockerException, Exception) as e:
        print(f"❌ Docker 컨테이너 재시작 오류: {e}")
        return False

def sanitize_filename(name):
    """파일 이름으로 사용할 수 없는 문자를 제거합니다."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.replace('..', '') # '..' 경로 이스케이프 방지
    return name

def upload_to_b2(local_file_path, bucket_name, b2_object_key, proxy_manager, max_retries=3):
    """로컬 파일을 Backblaze B2에 업로드합니다."""
    thread_id = threading.current_thread().name
    if not os.path.exists(local_file_path):
        print(f"❌ [{thread_id}] B2 업로드 실패: 파일이 존재하지 않음 - {local_file_path}")
        return False

    file_size = os.path.getsize(local_file_path)

    for attempt in range(max_retries):
        try:
            proxy_manager.wait_for_proxy() # 프록시 재시작 중이면 대기
            b2_config = Config(signature_version='s3v4')
            b2_client = boto3.client('s3', endpoint_url=B2_ENDPOINT_URL, aws_access_key_id=B2_KEY_ID, aws_secret_access_key=B2_APPLICATION_KEY, config=b2_config)

            print(f"🔼 [{thread_id}] B2 업로드 시작 (시도 {attempt + 1}/{max_retries}): {b2_object_key} (크기: {file_size/1024/1024:.1f}MB)")
            b2_client.upload_file(local_file_path, bucket_name, b2_object_key)
            print(f"✅ [{thread_id}] B2 업로드 성공: {b2_object_key}")
            return True

        except (NoCredentialsError, ClientError) as e:
            print(f"❌ [{thread_id}] B2 업로드 인증/클라이언트 오류 ({b2_object_key}): {e}")
            if isinstance(e, NoCredentialsError) or (isinstance(e, ClientError) and e.response.get('Error', {}).get('Code', 'Unknown') in ['InvalidAccessKeyId', 'SignatureDoesNotMatch']):
                return False

        except (ConnectionError, http.client.HTTPException, urllib.error.URLError, OSError) as e:
            print(f"⚠️ [{thread_id}] B2 업로드 네트워크 오류 (시도 {attempt + 1}/{max_retries}): {e}")

        except Exception as e:
            print(f"❌ [{thread_id}] B2 업로드 중 예상치 못한 오류 ({b2_object_key}): {type(e).__name__} - {e}")

        if attempt < max_retries - 1:
            wait_time = 2 ** attempt
            print(f"🕒 [{thread_id}] B2 업로드 재시도 전 {wait_time}초 대기...")
            time.sleep(wait_time)
        else:
            print(f"❌ [{thread_id}] B2 업로드 최종 실패: {b2_object_key}")
            return False
    return False

def delete_local_file(file_path, max_retries=3, force_cleanup=False):
    """로컬 파일을 삭제합니다."""
    if not file_path or not os.path.exists(file_path):
        return True # 파일이 없으면 이미 삭제된 것으로 간주

    for attempt in range(max_retries):
        try:
            if force_cleanup and os.name == 'nt': # Windows에서 읽기 전용 파일 강제 삭제
                import stat
                os.chmod(file_path, stat.S_IWRITE)
            os.remove(file_path)
            return True
        except (PermissionError, OSError) as e:
            print(f"⚠️ 파일 삭제 오류 (시도 {attempt + 1}/{max_retries}): {file_path} - {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                print(f"❌ 파일 삭제 최종 실패: {file_path} - {e}")
                return False
    return False

def get_videos_from_url_with_retry(url, proxies, max_retries, delay, proxy_manager, shutdown_manager):
    """주어진 URL에서 비디오 목록을 가져옵니다 (재시도 및 채널 처리 로직 수정 포함)."""
    for attempt in range(max_retries):
        if shutdown_manager.is_shutting_down(): return []
        try:
            proxy_manager.wait_for_proxy()
            if 'playlist' in url:
                return list(Playlist(url, proxies=proxies, client="ANDROID_VR").videos)
            
            elif any(s in url for s in ['/c/', '/channel/', '/user/', '/@']):
                c = Channel(url, proxies=proxies, client="ANDROID_VR")
                if not c.channel_id:
                    print(f"⚠️ 채널 ID를 찾을 수 없습니다: {url}")
                    return [] 

                uploads_playlist_id = 'UU' + c.channel_id[2:]
                uploads_playlist_url = f"https://www.youtube.com/playlist?list={uploads_playlist_id}"
                print(f"ℹ️ 채널 URL 감지. 업로드 재생목록으로 전환하여 비디오를 가져옵니다: {uploads_playlist_url}")
                return list(Playlist(uploads_playlist_url, proxies=proxies, client="ANDROID_VR").videos)
            else:
                return [YouTube(url, proxies=proxies, client="ANDROID_VR")]

        except BotDetection as e:
            print(f"🚨 BOT 감지. 프록시 재시작. (URL: {url[:70]}...)")
            proxy_manager.handle_restart(f"BOT 감지 ({e})")
        
        except PytubeFixError as e:
            print(f"❌ PytubeFix 오류: URL '{url}' 에서 비디오 목록 가져오기 실패 (시도 {attempt + 1}/{max_retries}): {e}")
            if "is unavailable" in str(e) or "private video" in str(e).lower() or "removed by uploader" in str(e).lower():
                print(f"⚠️ 비디오가 사용 불가/비공개/삭제됨. 목록에서 제외.")
                return [] 
            
        except (urllib.error.URLError, http.client.RemoteDisconnected, http.client.HTTPException, ConnectionError, socket.timeout) as e:
            print(f"⚠️ URL '{url}' 목록 가져오기 중 네트워크 오류 (시도 {attempt + 1}/{max_retries}): {e}")
        
        except Exception as e:
            print(f"❌ URL '{url}' 에서 비디오 목록 가져오기 실패 (시도 {attempt + 1}/{max_retries}): {e}")

        if attempt < max_retries - 1:
            time.sleep(delay * (attempt + 1)) # 지수 백오프
    return []

# [수정된 함수]
def download_thumbnail(yt_obj, thumbnail_path, proxies, max_retries=3):
    """
    YouTube 객체에서 썸네일을 다운로드합니다.
    [수정] 프록시 정보를 인자로 받아 처리하며, 스레드에 안전한 방식으로 구현합니다.
    """
    thread_id = threading.current_thread().name
    for attempt in range(max_retries):
        try:
            thumbnail_url = yt_obj.thumbnail_url
            if not thumbnail_url:
                print(f"⚠️ [{thread_id}] 썸네일 URL을 찾을 수 없습니다: {yt_obj.title}")
                return False

            # 프록시 설정에 따라 요청 핸들러 구성
            if proxies:
                proxy_handler = urllib.request.ProxyHandler(proxies)
                opener = urllib.request.build_opener(proxy_handler)
                # 각 스레드가 자신만의 opener를 사용하도록 install_opener 대신 opener.open 사용
                with opener.open(thumbnail_url) as response:
                    with open(thumbnail_path, 'wb') as out_file:
                        out_file.write(response.read())
            else:
                # 프록시가 없으면 표준 urlretrieve 사용
                urllib.request.urlretrieve(thumbnail_url, thumbnail_path)

            if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
                return True
            else:
                raise Exception("파일이 생성되지 않았거나 크기가 0입니다.")

        except (urllib.error.URLError, http.client.HTTPException, OSError, Exception) as e:
            print(f"⚠️ [{thread_id}] 썸네일 다운로드 오류 (시도 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
            else:
                print(f"❌ [{thread_id}] 썸네일 다운로드 최종 실패: {yt_obj.title}")
                return False
    return False

def process_video_item(video_meta, proxies_dict, proxy_manager, shutdown_manager, download_mode, completed_video_ids, completed_thumbnail_ids):
    """단일 비디오 항목을 처리합니다 (다운로드, 병합, B2 업로드)."""
    thread_id = threading.current_thread().name
    video_id, video_title, item_url = video_meta['id'], video_meta['title'], video_meta['url']

    is_video_completed = video_id in completed_video_ids
    is_thumbnail_completed = video_id in completed_thumbnail_ids

    do_video = download_mode in ['video', 'both'] and not is_video_completed
    do_thumbnail = download_mode in ['thumbnail', 'both'] and not is_thumbnail_completed

    if not do_video and not do_thumbnail:
        print(f"ℹ️ [{thread_id}] '{video_title}' (ID: {video_id})는 이미 완료되어 건너뜁니다.")
        return 

    active_threads = threading.active_count() - 1
    print(f"🔧 [{thread_id}] 스레드 시작: '{video_title}' (영상: {do_video}, 썸네일: {do_thumbnail}) (활성: {active_threads}개)")

    video_temp_path, audio_temp_path, merged_file_path, thumbnail_path = None, None, None, None
    yt = None

    try:
        # --- YouTube 객체 생성 (개선된 재시도 로직) ---
        for attempt in range(MAX_RETRIES):
            if shutdown_manager.is_shutting_down(): return
            try:
                proxy_manager.wait_for_proxy()
                yt = YouTube(item_url, proxies=proxies_dict, client="ANDROID_VR")
                yt.check_availability()
                break # 성공 시 루프 탈출
            
            except BotDetection as e:
                print(f"🚨 [{thread_id}] BOT 감지. 프록시 재시작. ({video_title})")
                proxy_manager.handle_restart(f"BOT 감지 ({e})")
            
            except PytubeFixError as e:
                if "is unavailable" in str(e) or "private video" in str(e).lower() or "removed by uploader" in str(e).lower():
                    print(f"🚨 [{thread_id}] 영상 사용 불가 또는 비공개/삭제됨. ({video_title}) 오류: {e}")
                    log_error(DB_PATH, video_id, item_url, video_title, video_meta.get('author', 'N/A'), f"PytubeFixError: {e}")
                    return # 복구 불가, 이 비디오 처리 종료
                else:
                    print(f"⚠️ [{thread_id}] PytubeFix 오류 (시도 {attempt + 1}/{MAX_RETRIES}): {e}")
            
            except (urllib.error.URLError, http.client.RemoteDisconnected, http.client.HTTPException, ConnectionError, socket.timeout) as e:
                print(f"⚠️ [{thread_id}] YouTube 객체 생성 중 네트워크 오류 (시도 {attempt + 1}/{MAX_RETRIES}): {e}")

            except Exception as e:
                print(f"⚠️ [{thread_id}] YouTube 객체 생성 실패 (시도 {attempt + 1}/{MAX_RETRIES}): {e}")

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1)) # 지수 백오프
            else:
                raise Exception(f"YouTube 객체 생성 최종 실패 후 재시도 중단")

        if not yt:
            raise Exception("YouTube 객체를 생성할 수 없습니다. 비디오를 처리할 수 없습니다.")

        channel_name_sane = sanitize_filename(video_meta['author'])
        output_dir = os.path.join(DOWNLOAD_BASE_DIR, f"[Channel] {channel_name_sane}")
        os.makedirs(output_dir, exist_ok=True)
        filename_base = f"[{channel_name_sane}] {video_title} ({video_id})"

        # --- 썸네일 처리 ---
        if do_thumbnail:
            thumbnail_path = os.path.join(output_dir, f"{filename_base}.jpg")
            print(f"🖼️ [{thread_id}] 썸네일 처리 시작: {video_title}")
            # [수정] download_thumbnail 호출 시 proxies_dict 전달
            if download_thumbnail(yt, thumbnail_path, proxies_dict):
                b2_key = os.path.relpath(thumbnail_path, DOWNLOAD_BASE_DIR).replace(os.path.sep, '/')
                if upload_to_b2(thumbnail_path, B2_BUCKET_NAME, b2_key, proxy_manager):
                    log_completion(DB_PATH, video_id, 'thumbnail', video_title, video_meta['author'])
                else:
                    print(f"❌ [{thread_id}] 썸네일 B2 업로드 실패: {video_title}")
                    log_error(DB_PATH, video_id, item_url, video_title, video_meta['author'], "썸네일 B2 업로드 실패")
            else:
                print(f"❌ [{thread_id}] 썸네일 다운로드 실패: {video_title}")
                log_error(DB_PATH, video_id, item_url, video_title, video_meta['author'], "썸네일 다운로드 실패")

            if thumbnail_path and os.path.exists(thumbnail_path): delete_local_file(thumbnail_path)
            thumbnail_path = None

        # --- 비디오 처리 ---
        if do_video:
            video_temp_path = os.path.join(output_dir, f"{video_id}_video.mp4")
            audio_temp_path = os.path.join(output_dir, f"{video_id}_audio.mp4")
            merged_file_path = os.path.join(output_dir, f"{filename_base}.mp4")
            print(f"🎬 [{thread_id}] 비디오 처리 시작: {video_title}")

            try:
                video_stream = None
                for res in PREFERRED_RESOLUTIONS:
                    stream_found = yt.streams.filter(adaptive=True, file_extension='mp4', resolution=res).first()
                    if stream_found:
                        video_stream = stream_found
                        print(f"  - [{thread_id}] '{res}' 해상도 비디오 스트림 선택됨.")
                        break

                if not video_stream:
                    video_stream = yt.streams.filter(adaptive=True, file_extension='mp4').order_by('resolution').desc().first()
                    if video_stream:
                        print(f"  - [{thread_id}] 선호 해상도를 찾을 수 없어, 최고 해상도 '{video_stream.resolution}' 선택됨.")
                    else:
                        raise Exception("사용 가능한 비디오 스트림이 없습니다.")

                audio_stream = yt.streams.get_audio_only()
                if not audio_stream: raise Exception("사용 가능한 오디오 스트림이 없습니다.")

                print(f"  - [{thread_id}] 비디오 다운로드 중... (해상도: {video_stream.resolution})")
                video_stream.download(output_path=output_dir, filename=os.path.basename(video_temp_path), max_retries=MAX_DOWNLOAD_RETRIES)
                print(f"  - [{thread_id}] 오디오 다운로드 중...")
                audio_stream.download(output_path=output_dir, filename=os.path.basename(audio_temp_path), max_retries=MAX_DOWNLOAD_RETRIES)

                if shutdown_manager.is_shutting_down(): raise KeyboardInterrupt

                print(f"  - [{thread_id}] 비디오/오디오 병합 중...")
                ffmpeg_command = ['ffmpeg', '-y', '-i', video_temp_path, '-i', audio_temp_path, '-c', 'copy', '-loglevel', 'error', merged_file_path]
                result = subprocess.run(ffmpeg_command, capture_output=True, text=True, encoding='utf-8')
                if result.returncode != 0: raise Exception(f"FFmpeg 병합 실패: {result.stderr}")

                delete_local_file(video_temp_path); video_temp_path = None
                delete_local_file(audio_temp_path); audio_temp_path = None

                b2_key = os.path.relpath(merged_file_path, DOWNLOAD_BASE_DIR).replace(os.path.sep, '/')
                if upload_to_b2(merged_file_path, B2_BUCKET_NAME, b2_key, proxy_manager):
                    log_completion(DB_PATH, video_id, 'video', video_title, video_meta['author'])
                else:
                    raise Exception("비디오 B2 업로드 실패")
            except Exception as e:
                raise e
            finally:
                if video_temp_path and os.path.exists(video_temp_path): delete_local_file(video_temp_path)
                if audio_temp_path and os.path.exists(audio_temp_path): delete_local_file(audio_temp_path)
                if merged_file_path and os.path.exists(merged_file_path): delete_local_file(merged_file_path)
                video_temp_path, audio_temp_path, merged_file_path = None, None, None

    except (KeyboardInterrupt, SystemExit):
        print(f"⚠️ [{thread_id}] '{video_title}' 처리 중단됨.")
    except Exception as e:
        print(f"❌ [{thread_id}] '{video_title}' 처리 중 최종 오류: {e}")
        log_error(DB_PATH, video_id, item_url, video_title, video_meta.get('author', 'N/A'), str(e))
    finally:
        force_cleanup = shutdown_manager.is_shutting_down()
        if video_temp_path and os.path.exists(video_temp_path): delete_local_file(video_temp_path, force_cleanup=force_cleanup)
        if audio_temp_path and os.path.exists(audio_temp_path): delete_local_file(audio_temp_path, force_cleanup=force_cleanup)
        if merged_file_path and os.path.exists(merged_file_path): delete_local_file(merged_file_path, force_cleanup=force_cleanup)
        if thumbnail_path and os.path.exists(thumbnail_path): delete_local_file(thumbnail_path, force_cleanup=force_cleanup)

        final_active_threads = threading.active_count() - 1
        print(f"🏁 [{thread_id}] 스레드 종료: '{video_title}' (활성: {final_active_threads}개)")

def get_initial_metadata(video_obj, proxies_dict, max_retries=3, proxy_manager=None, shutdown_manager=None, use_cache=False):
    """
    비디오 객체에서 초기 메타데이터를 가져옵니다 (재시도 포함).
    캐시 사용 여부에 따라 SQLite 캐시를 먼저 확인하고, 없으면 YouTube에서 가져와 캐시합니다.
    """
    video_id = video_obj.video_id

    if use_cache:
        cached_meta = get_metadata_from_cache(DB_PATH, video_id)
        if cached_meta:
            return cached_meta

    for attempt in range(max_retries):
        if shutdown_manager and shutdown_manager.is_shutting_down(): return None
        try:
            if proxy_manager: proxy_manager.wait_for_proxy()
            
            # 메타데이터 로드를 위해 속성에 접근
            _ = video_obj.title
            
            meta = {
                "id": video_obj.video_id,
                "title": sanitize_filename(video_obj.title),
                "author": video_obj.author,
                "url": video_obj.watch_url,
            }
            if use_cache:
                save_metadata_to_cache(DB_PATH, meta)
            return meta
        
        except BotDetection as e:
            print(f"🚨 BOT 감지. 프록시 재시작. (메타데이터 로드: {video_obj.watch_url})")
            if proxy_manager: proxy_manager.handle_restart(f"BOT 감지 ({e})")
        
        except PytubeFixError as e:
            print(f"❌ PytubeFix 오류: 메타데이터 로드 실패 (시도 {attempt + 1}/{max_retries}): {video_obj.watch_url} - {e}")
            if "is unavailable" in str(e) or "private video" in str(e).lower() or "removed by uploader" in str(e).lower():
                print(f"⚠️ 비디오가 사용 불가/비공개/삭제됨. 메타데이터 로드 중단.")
                return None
        
        except (urllib.error.URLError, http.client.RemoteDisconnected, http.client.HTTPException, ConnectionError, socket.timeout) as e:
             print(f"⚠️ 메타데이터 로드 중 네트워크 오류 (시도 {attempt + 1}/{max_retries}): {video_obj.watch_url} - {e}")

        except Exception as e:
            print(f"⚠️ 메타데이터 로드 실패 (시도 {attempt + 1}/{max_retries}): {video_obj.watch_url} - {e}")
        
        if attempt < max_retries - 1:
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
        else:
            print(f"❌ 메타데이터 로드 최종 실패: {video_obj.watch_url}")
            return None

# --- 메인 실행 로직 ---
def main():
    shutdown_manager = GracefulShutdown()
    parser = argparse.ArgumentParser(
        description="YouTube 비디오 및/또는 썸네일을 다운로드하여 Backblaze B2에 업로드합니다.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("input_path", help="처리할 URL 또는 URL 목록이 포함된 .txt 파일 경로")
    parser.add_argument("--start", type=int, default=1, help="목록에서 처리를 시작할 번호 (기본값: 1)")
    parser.add_argument(
        "--mode", type=str, choices=['video', 'thumbnail', 'both'], default='both',
        help="다운로드 모드를 선택합니다:\n"
             "  - video: 영상만 다운로드 및 업로드\n"
             "  - thumbnail: 썸네일만 다운로드 및 업로드\n"
             "  - both: 영상과 썸네일 모두 처리 (기본값)"
    )
    parser.add_argument(
        "--cache", action="store_true", help="메타데이터를 SQLite에 캐싱하고, 캐시된 데이터를 우선 사용합니다."
    )
    args = parser.parse_args()

    init_database(DB_PATH)

    proxies_dict = None
    proxy_manager = ProxyManager()

    if USE_PROXY:
        proxy_url_masked = f"{PROXY_PROTOCOL}://{PROXY_HOST_PORT}"
        if USE_PROXY_AUTH and PROXY_USERNAME and PROXY_PASSWORD:
            proxy_url = f"{PROXY_PROTOCOL}://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST_PORT}"
            proxies_dict = {"http": proxy_url, "https": proxy_url}
            proxy_url_masked = f"{PROXY_PROTOCOL}://{PROXY_USERNAME}:***@{PROXY_HOST_PORT}"
        else:
            proxy_url = f"{PROXY_PROTOCOL}://{PROXY_HOST_PORT}"
            proxies_dict = {"http": proxy_url, "https": proxy_url}

        print(f"⚠️ 프록시를 사용합니다: {proxy_url_masked}")

        print(f"🌐 프록시 헬스 체크 중: {PROXY_HOST_PORT}...")
        if not check_proxy_health(PROXY_HOST_PORT):
            print("❌ 프록시 서버가 작동하지 않습니다. 설정을 확인하고 다시 시도하세요."); sys.exit(1)
        print("✅ 프록시 헬스 체크 통과.")


    input_path = args.input_path
    target_urls = []
    if input_path.lower().endswith('.txt'):
        if os.path.exists(input_path):
            with open(input_path, 'r', encoding='utf-8') as f:
                target_urls = [line.strip() for line in f if line.strip()]
        else:
            print(f"❌ 오류: 파일 '{input_path}'를 찾을 수 없습니다."); sys.exit(1)
    else:
        target_urls = [input_path]
    if not target_urls: print("❌ 처리할 URL이 없습니다. 스크립트를 종료합니다."); sys.exit(1)

    print("\n" + "=" * 60 + "\n1. URL에서 비디오 목록을 병렬로 수집합니다...\n" + "=" * 60)
    all_video_objects = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        url_futures = {executor.submit(get_videos_from_url_with_retry, url, proxies_dict, MAX_RETRIES, RETRY_DELAY_SECONDS, proxy_manager, shutdown_manager): url for url in target_urls}

        for i, future in enumerate(as_completed(url_futures)):
            if shutdown_manager.is_shutting_down():
                print("\n[MAIN] 종료 신호 감지. URL 목록 수집 중단.")
                for f in url_futures: f.cancel()
                break
            url = url_futures[future]
            try:
                videos = future.result()
                if videos:
                    all_video_objects.extend(videos)
                    print(f"  -> URL '{url[:70]}...' 에서 {len(videos)}개 비디오 발견. (진행률: {i+1}/{len(target_urls)})")
                else:
                    print(f"  -> URL '{url[:70]}...' 에서 비디오를 찾을 수 없거나 오류 발생. (진행률: {i+1}/{len(target_urls)})")
            except Exception as exc:
                print(f"❌ URL '{url[:70]}...' 처리 중 오류 발생: {exc}")

    if not all_video_objects:
        print("❌ 처리할 비디오가 없습니다. 스크립트를 종료합니다."); sys.exit(0)

    print("\n" + "=" * 60 + "\n2. 모든 비디오의 메타데이터를 병렬로 미리 가져옵니다...\n" + "=" * 60)
    video_metadata_list = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        meta_futures = {executor.submit(get_initial_metadata, video_obj, proxies_dict, MAX_RETRIES, proxy_manager, shutdown_manager, args.cache): video_obj for video_obj in all_video_objects}

        for i, future in enumerate(as_completed(meta_futures)):
            if shutdown_manager.is_shutting_down():
                print("\n[MAIN] 종료 신호 감지. 메타데이터 수집 중단.")
                for f in meta_futures: f.cancel()
                break
            video_obj = meta_futures[future]
            try:
                meta = future.result()
                if meta:
                    video_metadata_list.append(meta)
            except Exception as exc:
                print(f"❌ 메타데이터 로드 중 오류 발생: {exc} (비디오: {video_obj.watch_url})")

            print(f"  - 메타데이터 로딩 진행률: {i+1}개 완료 / 총 {len(all_video_objects)}개")

    print(f"\n✅ 총 {len(video_metadata_list)}개 비디오의 메타데이터 로드 완료. (실패: {len(all_video_objects) - len(video_metadata_list)}개)")

    if not video_metadata_list:
        print("❌ 유효한 메타데이터를 가진 비디오가 없습니다. 스크립트를 종료합니다."); sys.exit(0)

    print("\n" + "=" * 60 + "\n3. 데이터베이스에서 완료된 항목을 확인합니다...\n" + "=" * 60)
    completed_video_ids, completed_thumbnail_ids = get_completed_ids(DB_PATH)
    print(f"✅ 확인 완료: 완료된 비디오 {len(completed_video_ids)}개, 썸네일 {len(completed_thumbnail_ids)}개")

    final_process_list = []
    if args.mode == 'video':
        final_process_list = [v for v in video_metadata_list if v['id'] not in completed_video_ids]
    elif args.mode == 'thumbnail':
        final_process_list = [v for v in video_metadata_list if v['id'] not in completed_thumbnail_ids]
    else: # both
        for v in video_metadata_list:
            is_video_done = v['id'] in completed_video_ids
            is_thumbnail_done = v['id'] in completed_thumbnail_ids
            if not is_video_done or not is_thumbnail_done:
                final_process_list.append(v)


    print(f"\nℹ️  총 {len(video_metadata_list)}개 중 새로운 처리 대상: {len(final_process_list)}개.")

    start_number = args.start
    if start_number > 1:
        if start_number > len(final_process_list):
            print(f"⚠️ 시작 번호({start_number})가 총 처리 대상({len(final_process_list)})보다 큽니다. 처리할 항목이 없습니다.")
            final_process_list = []
        else:
            final_process_list = final_process_list[start_number - 1:]
        print(f"ℹ️ 시작 번호({start_number}) 적용 후 처리 대상: {len(final_process_list)}개.")

    if not final_process_list:
        print("✅ 처리할 새로운 항목이 없습니다. 스크립트를 종료합니다."); sys.exit(0)

    print("\n" + "=" * 60 + f"\n5. 총 {len(final_process_list)}개 항목 병렬 처리 시작 (모드: {args.mode}, 최대 워커: {MAX_WORKERS})\n" + "=" * 60)

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_video_item, meta, proxies_dict, proxy_manager, shutdown_manager, args.mode, completed_video_ids, completed_thumbnail_ids): meta
                for meta in final_process_list
            }

            for i, future in enumerate(as_completed(futures)):
                if shutdown_manager.is_shutting_down():
                    print("\n[MAIN] 종료 신호 감지. 남은 작업을 취소합니다.")
                    for f in futures: f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    meta = futures[future]
                    print(f'CRITICAL: 작업 실행 중 예상치 못한 에러 발생: {exc} (비디오: {meta.get("title", "UNKNOWN")})')
                finally:
                    print(f"📊 진행률: {i+1}/{len(final_process_list)}")

    except KeyboardInterrupt:
        print("\n[MAIN] KeyboardInterrupt 감지. 모든 작업을 중단합니다.")
        shutdown_manager.shutdown_event.set()
    finally:
        if shutdown_manager.is_shutting_down():
            print("\n🕒 스크립트가 안전하게 종료되었습니다.")
        else:
            print("\n✨ 모든 작업이 정상적으로 완료되었습니다.")

if __name__ == "__main__":
    main()
