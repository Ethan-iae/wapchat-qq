import os
import json
import time
import requests
import html
import re
import base64
import io
import hashlib
import tempfile
import uuid
import urllib.parse
from PIL import Image
from flask import send_file
from collections import OrderedDict


class LimitedDict(OrderedDict):
    def __init__(self, max_size=1000, ttl_seconds=3600):
        super().__init__()
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

    def __setitem__(self, key, value):
        super().__setitem__(key, (value, time.time()))
        if len(self) > self.max_size:
            self.popitem(last=False)

    def __getitem__(self, key):
        value, timestamp = super().__getitem__(key)
        if time.time() - timestamp > self.ttl_seconds:
            del self[key]
            raise KeyError(key)
        return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False


from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    make_response,
    session,
)
import datetime
from datetime import timezone, timedelta

TZ_UTC8 = timezone(timedelta(hours=8))

from werkzeug.middleware.proxy_fix import ProxyFix
from flask_compress import Compress

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

app.config["COMPRESS_ALGORITHM"] = "gzip"
app.config["COMPRESS_MIN_SIZE"] = 100
app.config["COMPRESS_MIMETYPES"] = [
    "text/html",
    "application/json",
    "text/css",
    "application/javascript",
]
Compress(app)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "default_secret_key_please_change")
app.permanent_session_lifetime = datetime.timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "wapchat-drive")

s3_client = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
    try:
        s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
        )
        print("✅ 成功连接到 Cloudflare R2 网盘！")
    except Exception as e:
        print(f"❌ 初始化 R2 失败: {e}")

import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "")

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client["nokia_qq_db"]
    users_collection = db["users"]
    chat_collection = db["chat_history"]
    images_collection = db["images"]
    files_collection = db["files"]
    print("成功连接到 MongoDB Atlas 云数据库！")
except Exception as e:
    print(f" 无法连接到 MongoDB: {e}")


def load_users():
    """在应用启动时，从 MongoDB 把所有账号数据拉取到内存字典中"""
    temp_db = {}
    try:
        for user in users_collection.find():
            account = user["_id"]
            temp_db[account] = {
                "password": user.get("password"),
                "nickname": user.get("nickname"),
                "ip": user.get("ip"),
                "status": user.get("status"),
            }
    except Exception as e:
        print(f"读取云数据库失败: {e}")
    return temp_db


users_db = load_users()

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

try:
    import emoji
except ImportError:
    emoji = None


def filter_emoji(text):
    if not text:
        return text

    if emoji:
        try:
            text = emoji.demojize(text, language="zh")
            text = re.sub(r":([^:]+):", r"[\1]", text)
        except Exception as e:
            print(f"Emoji 转换失败: {e}")

    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27BF]", "", text)
    text = re.sub(r"[\ud800-\udfff]", "", text)

    return text


QQ_FACES = {}


def load_qq_faces():
    global QQ_FACES
    try:
        face_json_path = os.path.join(os.path.dirname(__file__), "face_config.json")
        with open(face_json_path, "r", encoding="utf-8") as f:
            face_data = json.load(f)

            for face in face_data.get("sysface", []):
                face_id = face.get("QSid")
                face_name = face.get("QDes", "")

                if face_id is not None and face_name:
                    clean_name = face_name.lstrip("/")
                    QQ_FACES[int(face_id)] = clean_name

        print(f"✅ 成功从 face_config.json 加载了 {len(QQ_FACES)} 个 QQ 原生表情！")
    except Exception as e:
        print(f"❌ 加载 face_config.json 失败，将使用兜底字典: {e}")
        QQ_FACES.update({14: "微笑", 13: "呲牙", 76: "强", 285: "汗"})


load_qq_faces()

KAOMOJI_LIST = []


def load_kaomojis():
    global KAOMOJI_LIST
    try:
        kmj_path = os.path.join(os.path.dirname(__file__), "all_output_result_kmj.txt")
        if os.path.exists(kmj_path):
            with open(kmj_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if parts and len(parts) >= 1:
                        kmj = parts[0]
                        if kmj and kmj not in KAOMOJI_LIST:
                            KAOMOJI_LIST.append(kmj)
            print(f"✅ 成功加载了 {len(KAOMOJI_LIST)} 个颜文字！")
        else:
            print("❌ 未找到 all_output_result_kmj.txt 文件，使用兜底颜文字。")
    except Exception as e:
        print(f"❌ 加载颜文字文件失败: {e}")

    if not KAOMOJI_LIST:
        KAOMOJI_LIST = ["(￣▽￣)", "(⊙_⊙)", "(T_T)", "╮(╯▽╰)╭", "(#`O′)"]


load_kaomojis()


def load_chat_history():
    """应用启动时，从 MongoDB 读取最新的 50 条聊天记录"""
    history = []
    max_id = 0
    try:
        docs = list(chat_collection.find({}, {"_id": 0}).sort("id", -1).limit(50))
        docs.reverse()
        history = docs
        if history:
            max_id = history[-1]["id"]
    except Exception as e:
        print(f"读取云端聊天记录失败: {e}")
    return history, max_id


def save_chat_message(msg_dict):
    """将单条消息存入 MongoDB，并自动清理旧数据防止撑爆免费容量"""
    try:
        chat_collection.insert_one(msg_dict.copy())
        if msg_dict["id"] > 5000:
            chat_collection.delete_many({"id": {"$lte": msg_dict["id"] - 5000}})
    except Exception as e:
        print(f"写入云端聊天记录失败: {e}")


def cleanup_old_images(max_limit=500):
    """
    检查图床容量，如果超过 max_limit，则清理最老旧的图片。
    这里我们将默认上限改小为 500 张。
    """
    try:
        img_count = images_collection.count_documents({})
        if img_count > max_limit:
            delete_count = img_count - max_limit
            old_images = (
                images_collection.find({}, {"_id": 1})
                .sort("time", 1)
                .limit(delete_count)
            )
            old_ids = [img["_id"] for img in old_images]

            if old_ids:
                images_collection.delete_many({"_id": {"$in": old_ids}})
                print(
                    f"[自动清理] 图床超过 {max_limit} 张，已自动清理 {len(old_ids)} 张远古老图！"
                )
    except Exception as e:
        print(f"图床自动清理失败: {e}")


chat_history, current_msg_id = load_chat_history()

TARGET_GROUP_ID = int(os.environ.get("TARGET_GROUP_ID", "0"))

qq_name_cache = LimitedDict(1000)


def get_qq_name(group_id, qq):
    qq_str = str(qq)
    if qq_str in qq_name_cache:
        return qq_name_cache[qq_str]
    try:
        resp = requests.post(
            "http://127.0.0.1:3000/get_group_member_info",
            json={"group_id": group_id, "user_id": int(qq)},
            timeout=1.5,
        ).json()

        if resp and resp.get("status") in ["ok", "success"]:
            data = resp.get("data", {})
            name = data.get("card") or data.get("nickname") or qq_str

            name = filter_emoji(name)
            name = re.sub(r"\[.*?\]", "", name).strip()
            if not name:
                name = qq_str

            qq_name_cache[qq_str] = name
            return name
    except Exception as e:
        print(f"获取QQ名字失败: {e}")
    return qq_str


recent_sent_messages = []


def get_qq_by_name(target_name):
    if not target_name:
        return None

    if target_name in ["全体成员", "all", "全体"]:
        return "all"

    if target_name in name_to_qq_cache:
        return name_to_qq_cache[target_name]

    try:
        resp = requests.post(
            "http://127.0.0.1:3000/get_group_member_list",
            json={"group_id": TARGET_GROUP_ID},
            timeout=2,
        ).json()

        if resp and resp.get("status") in ["ok", "success"]:
            members = resp.get("data", [])
            found_uid = None
            for member in members:
                uid = str(member.get("user_id", ""))
                card = member.get("card", "")
                nickname = member.get("nickname", "")

                clean_card = (
                    re.sub(r"\[.*?\]", "", filter_emoji(card)).strip() if card else ""
                )
                clean_nick = (
                    re.sub(r"\[.*?\]", "", filter_emoji(nickname)).strip()
                    if nickname
                    else ""
                )

                if clean_card:
                    name_to_qq_cache[clean_card] = uid
                if clean_nick:
                    name_to_qq_cache[clean_nick] = uid

                if target_name == clean_card or target_name == clean_nick:
                    found_uid = uid

            return found_uid
    except Exception as e:
        print(f"拉取群成员列表失败: {e}")

    return None


processed_notices_cache = LimitedDict(1000, 3600)

APP_START_TIME = int(time.time())

internal_file_cache = LimitedDict(100, 300)
recent_web_uploads = LimitedDict(100, 300)


@app.route("/api/internal_download/<file_id>")
def internal_download(file_id):
    client_ip = request.remote_addr or "127.0.0.1"
    if client_ip != "127.0.0.1":
        return "Forbidden", 403

    file_data = internal_file_cache.get(file_id)
    if not file_data:
        return "File not found or expired", 404

    return send_file(
        io.BytesIO(file_data["bytes"]),
        download_name=file_data["name"],
        as_attachment=True,
    )


name_to_qq_cache = LimitedDict(2000)

COOLDOWN_SECONDS = 5
MAX_MSG_LENGTH = 80
GLOBAL_COOLDOWN = 2
global_last_send_time = 0

ip_last_send_time = LimitedDict(1000)
ip_location_cache = LimitedDict(1000)
ip_username_cache = LimitedDict(1000)
ip_last_message = LimitedDict(1000)

online_sessions = {}
ONLINE_TIMEOUT = 30


def get_online_count():
    """获取当前在线人数，并自动清理已经掉线的死记录"""
    current_time = time.time()
    expired_accounts = [
        acc
        for acc, last_active in online_sessions.items()
        if current_time - last_active > ONLINE_TIMEOUT
    ]
    for acc in expired_accounts:
        del online_sessions[acc]
    return len(online_sessions)


def load_banned_words():
    try:
        with open("sensitive_words.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"Failed to load sensitive_words.txt: {e}")
        return []


BANNED_WORDS = load_banned_words()

NOKIA_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>WapChat QQ</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        #chat { padding: 6px; }
        .msg { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 0 0 4px 0; font-size: 12px; line-height: 1.3; word-wrap: break-word; }
        .user-msg { color: #000000; font-weight: bold; }
        .ai-msg { color: #333333; }
        .input-area { padding: 6px; background-color: #f5f5f5; border-bottom: 2px solid #000; }
        #userInput { width: 100%; height: 27px; padding: 2px 5px; font-size: 13px; border: 1px solid #000; vertical-align: middle; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; }
        #sendBtn { width: 100%; height: 27px; border: 1px solid #000; background-color: #dddddd; color: #000; font-weight: bold; font-size: 13px; vertical-align: middle; cursor: pointer; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; -webkit-appearance: none; }
        #sendBtn:active { background-color: #999999; color: #fff; }
        #attachBtn { width: 100%; height: 27px; border: 1px solid #000; background-color: #eee; color: #000; font-weight: bold; font-size: 13px; padding: 0; margin: 0; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; -webkit-appearance: none; }
        .action-at { color: blue; text-decoration: none; }
        .action-reply { color: #ff6600; text-decoration: none; }
        .reply-text { color: #ff6600; font-size: 11px; }
        .nav-links { float: right; font-size: 12px; font-weight: normal; }
        .nav-links a { color: blue; text-decoration: none; margin-left: 8px; }
    /* ]]> */
    </style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        .input-area { background-color: #111111 !important; border-bottom: 2px solid #555555 !important; }
        .msg { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        #userInput { background-color: #222222 !important; color: #ffffff !important; border-color: #555555 !important; }
        #sendBtn, #attachBtn { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        .nav-links a { color: blue !important; }
        .action-at { color: blue !important; }
        .action-reply { color: #ff6600 !important; }
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
    </style>
    {% endif %}
</head>
<body>
    <h2>
        <span class="nav-links">
            <a href="javascript:toggleMenu();">[选项▼]</a>
        </span>
        WapChat QQ 
        <a id="onlineCount" href="/online" style="font-size:12px; font-weight:normal; color:green; text-decoration:underline;">({{ online_count }}在线)</a>
    </h2>
    
    <div id="optionsMenu" style="display:none; position:absolute; top:30px; right:6px; width:50px; background:#fafafa; border:1px solid #000; padding:0; z-index:999; text-align:center;">
        <a href="/toggle_theme" style="display:block; font-size:12px; color:#888; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">{% if theme == 'dark' %}日间{% else %}夜间{% endif %}</a>
        <a href="/history" style="display:block; font-size:12px; color:#ff6600; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">历史</a>
        <a href="/drive" style="display:block; font-size:12px; color:green; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">网盘</a>
        <a href="{{ ai_url }}" style="display:block; font-size:12px; color:purple; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">智能</a>
        <a href="javascript:changeName(); toggleMenu();" style="display:block; font-size:12px; color:blue; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">改名</a>
        <a href="/logout" style="display:block; font-size:12px; color:red; text-decoration:none; padding:3px 0; margin:0;">注销</a>
    </div>
    
    <div id="attachMenu" style="display:none; position:absolute; top:65px; left:0; width:50px; background:#fafafa; border:1px solid #000; padding:0; z-index:999; text-align:center;">
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <a href="javascript:goToEmoji();" style="display:block; font-size:12px; color:#ff00ff; text-decoration:none; padding:3px 0; margin:0;">颜文字</a>
        </div>
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <div style="display:block; font-size:12px; color:blue; padding:3px 0; margin:0;">图片</div>
            <form id="uploadForm" action="/api/upload_image" method="POST" enctype="multipart/form-data" target="uploadTarget" style="position:absolute; top:0; left:0; width:100%; height:100%; margin:0; padding:0;">
                <input type="file" name="image" accept="image/*" onchange="handleAutoUpload('uploadForm', '图片');" style="position:absolute; top:0; left:0; width:100%; height:100%; opacity:0.01; filter:alpha(opacity=1); cursor:pointer; font-size:50px; outline:none;" />
            </form>
        </div>
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <div style="display:block; font-size:12px; color:green; padding:3px 0; margin:0;">文件</div>
            <form id="uploadFileForm" action="/api/upload_file" method="POST" enctype="multipart/form-data" target="uploadTarget" style="position:absolute; top:0; left:0; width:100%; height:100%; margin:0; padding:0;">
                <input type="file" name="file" onchange="handleAutoUpload('uploadFileForm', '文件');" style="position:absolute; top:0; left:0; width:100%; height:100%; opacity:0.01; filter:alpha(opacity=1); cursor:pointer; font-size:50px; outline:none;" />
            </form>
        </div>
        <iframe id="uploadTarget" name="uploadTarget" style="display:none;"></iframe>
    </div>

    <div class="input-area">
        <form onsubmit="sendMessage(); return false;">
            <table style="width: 100%; table-layout: fixed;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 66%; padding-right: 2px; vertical-align: middle;">
                        <input type="text" id="userInput" placeholder="在此输入..." spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off" />
                    </td>
                    <td style="width: 10%; padding-right: 2px; vertical-align: middle;">
                        <input type="button" id="attachBtn" value="▼" onclick="toggleAttachMenu();" />
                    </td>
                    <td style="width: 24%; vertical-align: middle;">
                        <input type="button" id="sendBtn" value="发送" onclick="sendMessage();" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="chat">
        {% for msg in history|reverse %}
        <div class="msg ai-msg" id="msg_{{ msg.id }}">
            <div style="overflow:hidden; margin-bottom:2px;">
                <b style="float:left;">{{ msg.get('sender_title', 'QQ群') }}:</b>
                {% if msg.pure_sender %}
                <span style="float:right;">
                    <a href="javascript:atUser('{{ msg.pure_sender | replace("'", "") | replace('"', '') }}');" class="action-at">[@]</a>
                    <a href="javascript:replyMsg('{{ msg.id }}', '{{ msg.pure_sender | replace("'", "") | replace('"', '') }}');" class="action-reply">[回]</a>
                </span>
                {% endif %}
            </div>
            <div style="clear:both;">{{ msg.text | safe }}</div>
        </div>
        {% endfor %}
        <div class="msg ai-msg"><b>系统:</b><br/>连接就绪，正在监听 QQ 群消息...</div>
    </div>

    <a name="bottom" id="bottom"></a>

    <script type="text/javascript">
/* <![CDATA[ */
        if (typeof XMLHttpRequest === "undefined") {
            window.XMLHttpRequest = function () {
                try { return new ActiveXObject("Msxml2.XMLHTTP.6.0"); } catch (e) {}
                try { return new ActiveXObject("Msxml2.XMLHTTP.3.0"); } catch (e) {}
                try { return new ActiveXObject("Microsoft.XMLHTTP"); } catch (e) {}
                throw new Error("此浏览器不支持 AJAX");
            };
        }
        if (!window.JSON) {
            window.JSON = {
                parse: function (sJSON) { try { return eval('(' + sJSON + ')'); } catch (e) { return null; } },
                stringify: function (v) {
                    var i, s = "", len;
                    if (v === null) return "null";
                    if (typeof v === 'number' || typeof v === 'boolean') return v.toString();
                    if (typeof v === 'string') return '"' + v.replace(/"/g, '\\"') + '"';
                    if (v instanceof Array) {
                        len = v.length;
                        for (i = 0; i < len; i++) { s += (s ? "," : "") + this.stringify(v[i]); }
                        return "[" + s + "]";
                    }
                    if (typeof v === 'object') {
                        for (i in v) { if (v.hasOwnProperty(i)) { s += (s ? "," : "") + '"' + i + '":' + this.stringify(v[i]); } }
                        return "{" + s + "}";
                    }
                    return "";
                }
            };
        }

        var chatBox = document.getElementById('chat');
        var input = document.getElementById('userInput');
        var btn = document.getElementById('sendBtn');

        function atUser(name) {
            if (!name) return;
            input.value += '[@' + name + '] ';
            input.focus();
        }
        function replyMsg(id, name) {
            if (!id) return;
            var val = input.value.replace(/^\[回复:\d+\]\s*/, '');
            if (name && val.indexOf('[@' + name + ']') === -1) {
                val = '[@' + name + '] ' + val;
            }
            input.value = '[回复:' + id + '] ' + val;
            input.focus();
        }
        function toggleMenu() {
            var menu = document.getElementById('optionsMenu');
            if (menu.style.display === 'none' || menu.style.display === '') {
                menu.style.display = 'block';
            } else {
                menu.style.display = 'none';
            }
        }
        var myUsername = "{{ saved_username }}";
        function getCookie(name) {
            var arr, reg = new RegExp("(^| )" + name + "=([^;]*)(;|$)");
            if (arr = document.cookie.match(reg)) return decodeURIComponent(arr[2]);
            else return "";
        }
        function goToEmoji() {
            var currentText = document.getElementById('userInput').value;
            document.cookie = "nokia_draft=" + encodeURIComponent(currentText) + "; path=/";
            var w = window.innerWidth || document.documentElement.clientWidth || document.body.clientWidth || 240;
            var h = window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight || 320;
            var cols = Math.floor(w / 80); 
            var rows = Math.floor((h - 90) / 32); 
            var limit = cols * rows;
            limit = Math.floor(limit * 0.8);
            if (limit < 8) limit = 8;
            if (limit > 500) limit = 500;
            document.cookie = "emoji_limit=" + limit + "; path=/";

            window.location.href = '/emojis';
        }
        var draftText = getCookie('nokia_draft');
        if (draftText !== "") {
            document.getElementById('userInput').value = draftText;
            document.cookie = "nokia_draft=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
            document.getElementById('userInput').focus();
        }
        function getElementLeft(element) {
            var actualLeft = element.offsetLeft;
            var current = element.offsetParent;
            while (current !== null) {
                actualLeft += current.offsetLeft;
                current = current.offsetParent;
            }
            return actualLeft;
        }
        function toggleAttachMenu() {
            var menu = document.getElementById('attachMenu');
            var btn = document.getElementById('attachBtn');
            
            if (menu.style.display === 'none' || menu.style.display === '') {
                var btnWidth = btn.offsetWidth;
                var btnLeft = getElementLeft(btn);
                var finalWidth = btnWidth < 50 ? 50 : btnWidth;
                var finalLeft = Math.floor(btnLeft - ((finalWidth - btnWidth) / 2));
                menu.style.width = finalWidth + 'px';
                menu.style.left = finalLeft + 'px';
                menu.style.right = 'auto';
                
                menu.style.display = 'block';
            } else {
                menu.style.display = 'none';
            }
        }
        function handleAutoUpload(formId, typeName) {
            var form = document.getElementById(formId);
            var fileInput = form.getElementsByTagName('input')[0];
            if (!fileInput.value) return; 
            form.submit();
            toggleAttachMenu();
            addMessage('系统', typeName + '正在后台发送中，请稍候...', true);
        }
        function uploadCallback(status, msg) {
            var menu = document.getElementById('attachMenu');
            menu.style.display = 'none';
            var imgInput = document.getElementById('uploadForm').getElementsByTagName('input')[0];
            if(imgInput) imgInput.value = '';
            
            var fileInput = document.getElementById('uploadFileForm').getElementsByTagName('input')[0];
            if(fileInput) fileInput.value = '';
            
            if (status === 'ok') {
                addMessage('系统', '发送成功！', true);
                if (pollTimer) clearTimeout(pollTimer);
                currentInterval = baseInterval;
                doPoll();
            } else {
                addMessage('系统', '发送失败: ' + msg, true);
            }
        }
        function changeName() {
            var newName = prompt("请输入您的新昵称：\\n(此操作不会更改您的登录账号)", myUsername);
            if (newName !== null) {
                newName = newName.replace(/^\s+|\s+$/g, '');
                if (newName !== '') {
                    var xhr = new XMLHttpRequest();
                    xhr.open('POST', '/api/change_name?t=' + new Date().getTime(), true);
                    xhr.setRequestHeader('Content-Type', 'application/json');
                    xhr.onreadystatechange = function () {
                        if (xhr.readyState === 4) {
                            if (xhr.status === 200) {
                                var res = JSON.parse(xhr.responseText);
                                if (res.status === 'ok') {
                                    myUsername = res.new_name;
                                    addMessage('系统', '昵称已成功更改为: ' + myUsername, true);
                                } else {
                                    addMessage('系统', '改名失败: ' + res.msg, true);
                                }
                            } else {
                                addMessage('系统', '网络请求失败，请稍后重试', true);
                            }
                        }
                    };
                    xhr.send(JSON.stringify({ new_name: newName }));
                } else {
                    addMessage('系统', '昵称不能为空，取消修改。', true);
                }
            }
        }
        var lastMsgId = {{ last_id }}; 

        function addMessage(sender_title, text, isAi, pure_sender, msg_id) {
            var div = document.createElement('div');
            div.className = 'msg ' + (isAi ? 'ai-msg' : 'user-msg');
            if (msg_id !== undefined && msg_id !== null) {
                div.id = 'msg_' + msg_id;
            }
            
            var actionLinks = '';
            if (pure_sender && msg_id) {
                var safeName = pure_sender.replace(/['"]/g, '');
                var atLink = '<a href="javascript:atUser(' + "'" + safeName + "'" + ');" class="action-at">[@]</a>';
                var repLink = '&nbsp;<a href="javascript:replyMsg(' + "'" + msg_id + "'" + ', ' + "'" + safeName + "'" + ');" class="action-reply">[回]</a>';
                actionLinks = '<span style="float:right;">' + atLink + repLink + '</span>';
            }
            div.innerHTML = '<div style="overflow:hidden; margin-bottom:2px;"><b style="float:left;">' + sender_title + ':</b>' + actionLinks + '</div><div style="clear:both;">' + text.replace(/\\n/g, '<br/>') + '</div>';
            
            if (chatBox.firstChild) {
                chatBox.insertBefore(div, chatBox.firstChild);
            } else {
                chatBox.appendChild(div);
            }
            window.scrollTo(0, 0);

            while (chatBox.children.length > 26) { 
                chatBox.removeChild(chatBox.children[chatBox.children.length - 1]); 
            }
        }

        function sendMessage() {
            var text = input.value;
            if (text === null || text === '') return;
            
            input.value = '';
            btn.disabled = true;
            btn.value = '发送中';

            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/sync?t=' + new Date().getTime(), true);
            xhr.setRequestHeader('Content-Type', 'application/json');

            xhr.onreadystatechange = function () {
                if (xhr.readyState === 4) {
                    btn.disabled = false;
                    btn.value = '发送';
                    if (xhr.status === 200) {
                        try {
                            var res = JSON.parse(xhr.responseText);
                            if (res.online_count !== undefined) {
                                var ocSpan = document.getElementById('onlineCount');
                                if (ocSpan) ocSpan.innerHTML = '(' + res.online_count + '在线)';
                            }
                            if (res.messages && res.messages.length > 0) {
                                for (var i = 0; i < res.messages.length; i++) {
                                    var m = res.messages[i];
                                    if (m.recall_target_id !== undefined && m.recall_target_id !== null) {
                                        var targetDiv = document.getElementById('msg_' + m.recall_target_id);
                                        if (targetDiv) {
                                            targetDiv.innerHTML = '<div style="color:#999; font-size:12px; margin-bottom:2px;"><i>[该消息已被撤回]</i></div>';
                                        }
                                    }
                                    if (m.id === undefined || m.id > lastMsgId) {
                                        var safeText = m.text.replace(/[\\uD800-\\uDBFF][\\uDC00-\\uDFFF]|[\\u2600-\\u27BF]/g, '');
                                        addMessage(m.sender_title, safeText, true, m.pure_sender, m.id);
                                        if (m.id !== undefined) {
                                            lastMsgId = m.id;
                                        }
                                    }
                                }
                            }
                            if (res.last_id !== undefined && res.last_id > lastMsgId) {
                                lastMsgId = res.last_id;
                            }
                        } catch (e) {
                            addMessage('系统', '解析数据失败', true);
                        }
                    } else {
                        addMessage('系统', '网络请求失败，状态码: ' + xhr.status, true);
                    }
                }
            };
            var requestData = JSON.stringify({ message: text, username: myUsername, last_id: lastMsgId });
            currentInterval = baseInterval;
            if (pollTimer) clearTimeout(pollTimer);
            pollTimer = setTimeout(doPoll, currentInterval);
            
            xhr.send(requestData);
        }

        var baseInterval = 5000;  
        var maxInterval = 25000;   
        var currentInterval = baseInterval;
        var pollTimer = null;
        var isPolling = false;

        function doPoll() {
            if (isPolling) return; 
            
            isPolling = true;
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/sync?t=' + new Date().getTime(), true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function () {
                if (xhr.readyState === 4) {
                    isPolling = false; 
                    var hasNewMessage = false; 

                    if (xhr.status === 200) {
                        try {
                            var res = JSON.parse(xhr.responseText);
                            if (res.online_count !== undefined) {
                                var ocSpan = document.getElementById('onlineCount');
                                if (ocSpan) ocSpan.innerHTML = '(' + res.online_count + '在线)';
                            }
                            if (res.messages && res.messages.length > 0) {
                                for (var i = 0; i < res.messages.length; i++) {
                                    var m = res.messages[i];
                                    if (m.recall_target_id !== undefined && m.recall_target_id !== null) {
                                        var targetDiv = document.getElementById('msg_' + m.recall_target_id);
                                        if (targetDiv) {
                                            targetDiv.innerHTML = '<div style="color:#999; font-size:12px; margin-bottom:2px;"><i>[该消息已被撤回]</i></div>';
                                        }
                                    }
                                    if (m.id === undefined || m.id > lastMsgId) {
                                        var safeText = m.text.replace(/[\\uD800-\\uDBFF][\\uDC00-\\uDFFF]|[\\u2600-\\u27BF]/g, '');
                                        addMessage(m.sender_title, safeText, true, m.pure_sender, m.id);
                                        if (m.id !== undefined) {
                                            lastMsgId = m.id;
                                            hasNewMessage = true; 
                                        }
                                    }
                                }
                            }
                            if (res.last_id !== undefined && res.last_id > lastMsgId) {
                                lastMsgId = res.last_id;
                            }
                        } catch (e) {}
                    }
                    if (hasNewMessage) {
                        currentInterval = baseInterval;
                    } else {
                        currentInterval = Math.min(currentInterval + 2000, maxInterval);
                    }
                    pollTimer = setTimeout(doPoll, currentInterval);
                }
            };
            xhr.send(JSON.stringify({ message: "", last_id: lastMsgId })); 
        }
        pollTimer = setTimeout(doPoll, currentInterval);
    /* ]]> */
</script>
</body>
</html>
"""

LOGIN_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>身份验证</title>
    <style type="text/css">
/* <![CDATA[ */
        
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
      
        .content { width: 95%; margin: 3px auto; }
     
        .msg { color: #d00; font-size: 12px; margin: 0 0 1px 0; font-weight: bold; line-height: 1.1; }
        
        label { font-size: 12px; font-weight: bold; display: block; margin-top: 2px; margin-bottom: 2px; }
        
        input[type="text"], input[type="password"] { width: 100%; height: 27px; border: 1px solid #000; margin: 0 0 5px 0; padding: 2px 5px; font-size: 13px; display: block; border-radius: 0; }
        input[type="submit"] { width: 100%; height: 27px; border: 1px solid #000; background-color: #dddddd; color: #000; font-weight: bold; font-size: 13px; margin-top: 8px; cursor: pointer; display: block; border-radius: 0; -webkit-appearance: none; }
        input[type="submit"]:active { background-color: #999999; color: #fff; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
       
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
        
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        
        #attachBtn { background-color: #222222 !important; }
       
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
       
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
      
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>WapChat QQ</h2>
    <div class="content">
        {% if error %}<div class="msg">{{ error }}</div>{% endif %}
        {% if success %}<div class="msg" style="color: green;">{{ success }}</div>{% endif %}
        <form method="POST" action="/login">
            <label>登录账号(纯英数字):</label>
            <input type="text" name="account" required="required" spellcheck="false" autocapitalize="off" autocomplete="off" />
            <label>终端密码:</label>
            <input type="password" name="password" required="required" />
            <input type="submit" name="login_btn" value="登 录" />
            <input type="submit" name="register_btn" value="注册新账号" />
        </form>
    </div>
</body>
</html>
"""

HISTORY_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>聊天历史记录</title>
    <style type="text/css">
/* <![CDATA[ */
      
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
      
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        
        #history-content { padding: 6px; }

        .search-bar input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .search-bar input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        .msg { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 0 0 4px 0; font-size: 12px; line-height: 1.3; word-wrap: break-word; }
        .ai-msg { color: #333333; }
     
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 20px; vertical-align: middle; box-sizing: border-box; -webkit-box-sizing: border-box; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
       
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
    
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
     
        #attachBtn { background-color: #222222 !important; }
        
   
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        
 
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
      
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
       
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>历史记录
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div class="search-bar" style="width: 95%; margin: 2px auto;">
        <form action="/history" method="GET" style="margin:0; padding:0;">
            <table style="width: 100%;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 75%; padding-right: 2px;">
                        <input type="text" name="q" value="{{ q }}" placeholder="搜点什么..." style="width: 100%; height: 22px; border: 1px solid #000; font-size: 12px; margin: 0; border-radius: 0; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                    <td style="width: 25%;">
                        <input type="submit" value="搜索" style="width: 100%; height: 22px; border: 1px solid #000; background-color: #eee; font-size: 12px; margin: 0; cursor: pointer; border-radius: 0; -webkit-appearance: none; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="history-content">
        {% if not messages %}
            <div class="msg">暂无更多历史记录...</div>
        {% endif %}
        
        {% for msg in messages %}
        <div class="msg ai-msg"><b>{{ msg.get('sender_title', 'QQ群') }}</b><br/>{{ msg.text | safe }}</div>
        {% endfor %}
    </div>

    <div class="nav">
        {% if page > 1 %}
            <a href="/history?page={{ page - 1 }}{% if q %}&amp;q={{ q }}{% endif %}">上一页</a>
        {% endif %}
        
        <form action="/history" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            {% if q %}
            <input type="hidden" name="q" value="{{ q }}" />
            {% endif %}
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
            
            <span style="display:inline-block; vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            
            <input type="submit" value="跳" style="height:22px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}
            <a href="/history?page={{ page + 1 }}{% if q %}&amp;q={{ q }}{% endif %}">下一页</a>
        {% endif %}
    </div>
</body>
</html>
"""

ONLINE_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>在线人员</title>
    <style type="text/css">
/* <![CDATA[ */
  
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        
   
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        

        #online-content { padding: 6px; }
        .user-item { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 4px 0; font-size: 13px; line-height: 1.3; }
        .nickname { font-weight: bold; color: #000000; }
        .account { font-size: 11px; color: #666666; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
      
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        
    
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
   
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
        
      
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
       
        #attachBtn { background-color: #222222 !important; }
        
     
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
       
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
      
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
       
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>在线名单 ({{ users|length }}人)
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div id="online-content">
        {% if not users %}
            <div class="user-item">当前没有其他用户在线...</div>
        {% else %}
            {% for u in users %}
            <div class="user-item">
                <span style="color:green;">●</span> 
                <span class="nickname">{{ u.nickname }}</span> 
                <span class="account">({{ u.account }})</span>
                {% if u.is_me %} <span style="font-size:11px; color:blue;">[我自己]</span> {% endif %}
            </div>
            {% endfor %}
        {% endif %}
    </div>
</body>
</html>
"""

EMOJI_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>选择颜文字</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        #emoji-content { padding: 6px; text-align: center; }
        
        .kmj-btn { display: inline-block; padding: 3px 5px; margin: 3px; border: 1px solid #999; background: #eee; text-decoration: none; color: #000; font-size: 12px; line-height: 1.2; }
        .kmj-btn:active { background: #ccc; }
        
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 22px; vertical-align: middle; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        #emoji-content { background: #111111 !important; border-color: #555555 !important; }
        .kmj-btn { border: 1px solid #555; background: #333; color: #ddd; }
        .kmj-btn:active { background: #555; }
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
    
        .nav input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .nav input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>颜文字 ({{ page }}/{{ total_pages }})
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[取消返回]</a>
        </span>
    </h2>

    <div id="emoji-content">
        {% for kmj in emojis %}
            <a href="javascript:selectEmoji('{{ kmj.encoded }}');" class="kmj-btn">{{ kmj.raw }}</a>
        {% endfor %}
    </div>

    <div class="nav">
        {% if page > 1 %}<a href="/emojis?page={{ page - 1 }}">上一页</a>{% endif %}
        
        <form action="/emojis" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; line-height:20px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0;" />
            <span style="vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            <input type="submit" value="跳" style="height:22px; line-height:20px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}<a href="/emojis?page={{ page + 1 }}">下一页</a>{% endif %}
    </div>

    <script type="text/javascript">
/* <![CDATA[ */
        function getCookie(name) {
            var arr, reg = new RegExp("(^| )" + name + "=([^;]*)(;|$)");
            if (arr = document.cookie.match(reg)) return decodeURIComponent(arr[2]);
            else return "";
        }
        function selectEmoji(encodedEmoji) {
            var emoji = decodeURIComponent(encodedEmoji);
            var draft = getCookie("nokia_draft");
            draft = draft + emoji; 
            document.cookie = "nokia_draft=" + encodeURIComponent(draft) + "; path=/";
            window.location.href = "/";
        }
    /* ]]> */
</script>
</body>
</html>
"""

DRIVE_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>群网盘</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
      
        #drive-content { padding: 6px; }
        
        .file-item { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 4px 0; font-size: 13px; line-height: 1.3; word-wrap: break-word;}
        .file-name { font-weight: bold; color: #000000; }
        .file-meta { font-size: 11px; color: #666666; margin-top: 2px;}
        .download-btn { color: blue; text-decoration: underline; font-weight: bold; }

        .search-bar input[type="text"] { background-color: #ffffff; color: #000000; border-color: #000000; }
        .search-bar input[type="submit"] { background-color: #eeeeee; color: #000000; border-color: #000000; }
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 20px; vertical-align: middle; box-sizing: border-box; -webkit-box-sizing: border-box; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        #drive-content { background: #111111 !important; border-color: #555555 !important; }
        .file-item { border-bottom: 1px dashed #333333 !important; }
        .file-name { color: #dddddd !important; }
        .file-meta { color: #888888 !important; }
        .download-btn { color: blue !important; }
  
        .search-bar input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .search-bar input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>群网盘
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div class="search-bar" style="width: 95%; margin: 2px auto;">
        <form action="/drive" method="GET" style="margin:0; padding:0;">
            <table style="width: 100%;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 75%; padding-right: 2px;">
                        <input type="text" name="q" value="{{ q }}" placeholder="搜文件名或上传者..." style="width: 100%; height: 22px; border: 1px solid #000; font-size: 12px; margin: 0; border-radius: 0; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                    <td style="width: 25%;">
                        <input type="submit" value="搜索" style="width: 100%; height: 22px; border: 1px solid #000; background-color: #eee; font-size: 12px; margin: 0; cursor: pointer; border-radius: 0; -webkit-appearance: none; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="drive-content">
        {% if not files %}
            <div class="file-item">网盘空空如也，或者没有搜到文件哦。</div>
        {% else %}
            {% for f in files %}
            <div class="file-item">
                <div class="file-name">{{ f.filename }}</div>
                <div class="file-meta">
                    上传者: {{ f.uploader }} | 大小: {{ f.size_mb }}MB <br/>
                    时间: {{ f.upload_time }}
                    <span style="float: right;">
                        <a href="/api/download_file?id={{ f._id }}" class="download-btn">[下载]</a>
                    </span>
                </div>
            </div>
            {% endfor %}
        {% endif %}
    </div>

    <div class="nav">
        {% if page > 1 %}
            <a href="/drive?page={{ page - 1 }}{% if q %}&amp;q={{ q }}{% endif %}">上一页</a>
        {% endif %}
        
        <form action="/drive" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            {% if q %}
            <input type="hidden" name="q" value="{{ q }}" />
            {% endif %}
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
            
            <span style="display:inline-block; vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            
            <input type="submit" value="跳" style="height:22px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}
            <a href="/drive?page={{ page + 1 }}{% if q %}&amp;q={{ q }}{% endif %}">下一页</a>
        {% endif %}
    </div>
</body>
</html>
"""


def get_real_ip(req):
    cf_custom_ip = req.headers.get("X-Real-IP-Custom")
    if cf_custom_ip:
        return cf_custom_ip.split(",")[0].strip()

    cf_ip = req.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.split(",")[0].strip()

    forwarded_for = req.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return req.remote_addr or "127.0.0.1"


def get_ip_location(ip, req):
    if (
        not ip
        or ip in ["127.0.0.1", "localhost", "0.0.0.0"]
        or ip.startswith("192.168.")
        or ip.startswith("10.")
        or ip.startswith("172.")
    ):
        return "未知地域"

    if ip in ip_location_cache:
        return ip_location_cache[ip]

    location = ""
    is_ipv6 = ":" in ip
    pro, city = "", ""

    if not is_ipv6:
        try:
            url = f"https://whois.pconline.com.cn/ipJson.jsp?ip={ip}&json=true"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = requests.get(url, headers=headers, timeout=2)
            response.encoding = "gbk"
            resp_data = response.json()

            pro = resp_data.get("pro", "").strip()
            city = resp_data.get("city", "").strip()
        except Exception:
            pass

    if not pro:
        try:
            fallback_url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            fallback_resp = requests.get(fallback_url, timeout=2).json()
            if fallback_resp.get("status") == "success":
                country = fallback_resp.get("country", "")
                if country == "中国":
                    pro = fallback_resp.get("regionName", "")
                    city = fallback_resp.get("city", "")
                else:
                    c_city = fallback_resp.get("city", "")
                    if c_city and c_city != country:
                        location = f"{country}-{c_city}"
                    else:
                        location = country
        except Exception:
            pass

    if pro and not location:
        pro = (
            pro.replace("省", "")
            .replace("市", "")
            .replace("自治区", "")
            .replace("维吾尔", "")
            .replace("回族", "")
            .replace("壮族", "")
        )
        city = city.replace("市", "")

        direct_cities = ["北京", "上海", "天津", "重庆"]
        special_admin = ["香港", "澳门", "台湾"]
        autonomous = ["内蒙古", "广西", "西藏", "宁夏", "新疆"]

        if pro in direct_cities:
            location = f"{pro}市"
        elif pro in special_admin:
            location = "台湾省" if pro == "台湾" else pro
        elif pro in autonomous:
            location = f"{pro}区{city}市" if city else f"{pro}区"
        else:
            location = f"{pro}省{city}市" if city else f"{pro}省"

    if not location:
        cf_country = req.headers.get("X-CF-Country")
        cf_region = req.headers.get("X-CF-Region")

        if cf_country == "CN":
            location = f"中国-{cf_region}" if cf_region else "中国"
        elif cf_country:
            location = f"海外({cf_country})"
        else:
            location = "未知地域"

    if location != "未知地域":
        ip_location_cache[ip] = location

    return location


@app.route("/login", methods=["GET", "POST"])
def login_page():
    theme = request.cookies.get("nokia_theme", "light")
    if request.method == "GET":
        account = session.get("nokia_account")

        if (
            account
            and account in users_db
            and users_db[account].get("status") != "pending"
        ):
            return redirect(url_for("index"))

        return render_template_string(LOGIN_HTML, theme=theme)

    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()

    if not account or not password:
        return render_template_string(
            LOGIN_HTML, error="账号和密码不能为空！", theme=theme
        )

    if not re.match(r"^[A-Za-z0-9_]{3,15}$", account):
        return render_template_string(
            LOGIN_HTML, error="账号只能包含3-15位字母、数字或下划线！", theme=theme
        )

    if "register_btn" in request.form:
        MAX_TOTAL_ACCOUNTS = 500
        if len(users_db) >= MAX_TOTAL_ACCOUNTS:
            return render_template_string(
                LOGIN_HTML, error="服务器名额已满，暂停新用户注册！", theme=theme
            )

        if account in users_db:
            return render_template_string(
                LOGIN_HTML, error="该账号已被注册，请直接登录！", theme=theme
            )

        client_ip = get_real_ip(request)
        MAX_ACCOUNTS_PER_IP = 1

        ip_reg_count = sum(
            1 for user_info in users_db.values() if user_info.get("ip") == client_ip
        )

        if ip_reg_count >= MAX_ACCOUNTS_PER_IP:
            return render_template_string(
                LOGIN_HTML,
                error="您的网络(IP)已达到注册上限，请勿重复注册！",
                theme=theme,
            )

        users_db[account] = {
            "password": password,
            "nickname": account,
            "ip": client_ip,
            "status": "pending",
        }
        try:
            users_collection.update_one(
                {"_id": account}, {"$set": users_db[account]}, upsert=True
            )
        except Exception as e:
            print(f"写入云数据库单用户失败: {e}")

        verify_base_url = os.environ.get("VERIFY_URL", "")
        cloudflare_verify_url = f"{verify_base_url}/?account={account}"
        return redirect(cloudflare_verify_url)

    else:
        if account not in users_db or users_db[account]["password"] != password:
            return render_template_string(
                LOGIN_HTML, error="账号或密码错误！", theme=theme
            )

        if users_db[account].get("status") == "pending":
            return render_template_string(
                LOGIN_HTML, error="账号正在审核中，请等待管理员人工通过！", theme=theme
            )

        session.permanent = True
        session["nokia_account"] = account
        return redirect(url_for("index"))


@app.route("/logout", methods=["GET"])
def logout():
    session.pop("nokia_account", None)
    return redirect(url_for("login_page"))


@app.route("/history", methods=["GET"])
def view_history():
    account = session.get("nokia_account")

    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()

    if page < 1:
        page = 1

    per_page = 25

    query = {}
    if q:
        query["$or"] = [
            {"pure_text": {"$regex": q, "$options": "i"}},
            {"pure_sender": {"$regex": q, "$options": "i"}},
        ]

    total_msgs = chat_collection.count_documents(query)
    total_pages = (total_msgs + per_page - 1) // per_page if total_msgs > 0 else 1

    if page > total_pages:
        page = total_pages

    skip_count = (page - 1) * per_page

    docs = list(
        chat_collection.find(query, {"_id": 0})
        .sort("id", -1)
        .skip(skip_count)
        .limit(per_page)
    )

    docs.reverse()

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(
        HISTORY_HTML,
        messages=docs,
        page=page,
        total_pages=total_pages,
        theme=theme,
        q=q,
    )


@app.route("/online", methods=["GET"])
def view_online_users():
    account = session.get("nokia_account")

    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return redirect(url_for("login_page"))

    get_online_count()

    active_users = []
    for acc in online_sessions.keys():
        nickname = users_db.get(acc, {}).get("nickname", acc)
        nickname = filter_emoji(nickname)

        active_users.append(
            {
                "account": acc,
                "nickname": nickname,
                "is_me": (acc == account),
            }
        )

    active_users.sort(key=lambda x: (not x["is_me"], x["account"]))

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(ONLINE_HTML, users=active_users, theme=theme)


@app.route("/emojis", methods=["GET"])
def view_emojis():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    try:
        per_page = int(request.cookies.get("emoji_limit", 40))
    except ValueError:
        per_page = 40

    total_kmj = len(KAOMOJI_LIST)
    total_pages = (total_kmj + per_page - 1) // per_page if total_kmj > 0 else 1
    if page > total_pages:
        page = total_pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    current_emojis = KAOMOJI_LIST[start_idx:end_idx]

    safe_emojis = [{"raw": k, "encoded": urllib.parse.quote(k)} for k in current_emojis]

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(
        EMOJI_HTML, emojis=safe_emojis, page=page, total_pages=total_pages, theme=theme
    )


@app.route("/toggle_theme", methods=["GET"])
def toggle_theme():
    current_theme = request.cookies.get("nokia_theme", "light")
    new_theme = "dark" if current_theme == "light" else "light"

    next_url = request.referrer or url_for("index")

    resp = make_response(redirect(next_url))
    resp.set_cookie("nokia_theme", new_theme, max_age=31536000)
    return resp


ADMIN_SECRET_TOKEN = os.environ.get(
    "ADMIN_SECRET_TOKEN", "default_admin_token_please_change"
)

WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")


@app.route("/admin/approve", methods=["GET"])
def admin_approve():
    token = request.args.get("token")
    account = request.args.get("account")

    if token != ADMIN_SECRET_TOKEN:
        return "权限拒绝：你的管理员 Token 不对，想黑我？没门！", 403

    if not account:
        return " 错误：你没有告诉我要审核哪个账号呀 (缺少 account 参数)。", 400

    user_data = users_db.get(account)
    if not user_data:
        try:
            doc = users_collection.find_one({"_id": account})
            if doc:
                user_data = doc
                users_db[account] = doc
        except Exception as e:
            print(f"管理员审核时查询云数据库失败: {e}")

    if not user_data:
        return f"错误：我的数据库里根本找不到 {html.escape(account)} 这个账号！", 404

    if user_data.get("status") == "pending":
        user_data["status"] = "active"
        if account in users_db:
            users_db[account]["status"] = "active"
        try:
            update_data = dict(user_data)
            if "_id" in update_data:
                del update_data["_id"]
            users_collection.update_one(
                {"_id": account}, {"$set": update_data}, upsert=True
            )
        except Exception as e:
            print(f"管理员审核存入云端单用户失败: {e}")
        return (
            f"成功：账号 {html.escape(account)} 已经通过审核！他现在可以去设备上登录了。",
            200,
        )
    else:
        return (
            f"提示：账号 {html.escape(account)} 现在的状态不是待审核，可能你之前已经点过通过了。",
            200,
        )


@app.route("/admin/reject", methods=["GET"])
def admin_reject():
    token = request.args.get("token")
    account = request.args.get("account")

    if token != ADMIN_SECRET_TOKEN:
        return " 权限拒绝：Token 不对！", 403

    if not account:
        return " 错误：缺少 account 参数。", 400

    if account in users_db:
        del users_db[account]

    try:
        users_collection.delete_one({"_id": account})
    except Exception as e:
        print(f"管理员拒绝删除云端单用户失败: {e}")
    return f"成功：已彻底拒绝并删除账号 {html.escape(account)} ！名额已释放。", 200


@app.route("/admin/delete_file", methods=["GET"])
def admin_delete_file():
    token = request.args.get("token")
    file_id = request.args.get("id")

    if token != ADMIN_SECRET_TOKEN:
        return "权限拒绝：Token 不对，你不是管理员！", 403

    if not file_id:
        return "错误：缺少 id 参数。", 400

    file_doc = files_collection.find_one({"_id": file_id})
    if not file_doc:
        return f"提示：找不到 ID 为 {file_id} 的文件，可能已经被删除了。", 404

    if "r2_key" in file_doc and s3_client:
        try:
            s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=file_doc["r2_key"])
            print(f"已从 R2 删除物理文件: {file_doc['r2_key']}")
        except Exception as e:
            print(f"从 R2 删除文件失败: {e}")
            return f"从 R2 删除物理文件失败，请检查 Koyeb 日志: {e}", 500

    try:
        files_collection.delete_one({"_id": file_id})
        return (
            f"成功：已彻底删除文件【{file_doc.get('filename', '未知文件')}】！释放了空间。",
            200,
        )
    except Exception as e:
        return f"删除数据库记录失败: {e}", 500


@app.route("/api/change_name", methods=["POST"])
def change_name_api():
    account = session.get("nokia_account")
    if not account or account not in users_db:
        return jsonify({"status": "error", "msg": "登录已失效，请重新登录。"})

    data = request.get_json(silent=True) or {}
    new_name = data.get("new_name", "").strip()
    new_name = filter_emoji(new_name)[:12]

    clean_username = re.sub(r"[^\w\u4e00-\u9fa5]", "", new_name).lower()
    if any(banned_word.lower() in clean_username for banned_word in BANNED_WORDS):
        return jsonify({"status": "error", "msg": "昵称包含违规词汇！"})

    if not new_name:
        return jsonify({"status": "error", "msg": "昵称不能为空或全为非法字符！"})

    users_db[account]["nickname"] = new_name
    try:
        users_collection.update_one({"_id": account}, {"$set": {"nickname": new_name}})
    except Exception as e:
        print(f"修改昵称同步云端单用户失败: {e}")
    return jsonify({"status": "ok", "new_name": new_name})


@app.route("/api/upload_image", methods=["POST"])
def upload_image():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "权限不足或未登录");/* ]]> */</script>'

    file = request.files.get("image")
    if not file or file.filename == "":
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "未选择任何照片");/* ]]> */</script>'

    try:
        img = Image.open(file)
        if img.mode != "RGB":
            img = img.convert("RGB")

        max_size = 800
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        img_io = io.BytesIO()
        img.save(img_io, "JPEG", quality=80)

        b64_data = base64.b64encode(img_io.getvalue()).decode("utf-8")
        cq_code = f"[CQ:image,file=base64://{b64_data}]"

        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        full_message = f"[{location} - {username}]\n{cq_code}"

        requests.post(
            "http://127.0.0.1:3000/send_group_msg",
            json={"group_id": TARGET_GROUP_ID, "message": full_message},
            timeout=8,
        )

        img_id = hashlib.md5(b64_data.encode("utf-8")).hexdigest()[:16]
        images_collection.insert_one(
            {"_id": img_id, "data": b64_data, "time": time.time()}
        )

        cleanup_old_images(500)

        global current_msg_id, chat_history
        current_msg_id += 1
        current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

        safe_user = html.escape(username)
        web_display_text = f"[网页-{location}]{safe_user}: [图片] <a href='/view_image?b64={img_id}' target='_blank' class='action-at'>[查看]</a>"

        new_msg = {
            "id": current_msg_id,
            "sender_title": f"消息 {current_time_str}",
            "text": web_display_text,
            "pure_sender": username,
            "pure_text": "[图片]",
        }

        chat_history.append(new_msg)
        save_chat_message(new_msg)

        if len(chat_history) > 50:
            chat_history.pop(0)

        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("ok", "");/* ]]> */</script>'

    except Exception as e:
        print(f"上传图片失败: {e}")
        return f'<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "文件处理失败");/* ]]> */</script>'


@app.route("/api/upload_file", methods=["POST"])
def upload_file():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "权限不足或未登录");/* ]]> */</script>'

    file = request.files.get("file")
    if not file or file.filename == "":
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "未选择任何文件");/* ]]> */</script>'

    try:
        filename = file.filename
        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        recent_web_uploads[filename] = username

        file_bytes = file.read()
        if not file_bytes:
            return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "不能发送空文件");/* ]]> */</script>'

        import uuid

        file_id = str(uuid.uuid4())
        internal_file_cache[file_id] = {"bytes": file_bytes, "name": filename}

        uploader_name = users_db[account].get("nickname", account)

        try:
            if s3_client:
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=file_id,
                    Body=file_bytes,
                    ContentType=file.content_type,
                )

                files_collection.insert_one(
                    {
                        "_id": file_id,
                        "filename": filename,
                        "r2_key": file_id,
                        "size": len(file_bytes),
                        "uploader": uploader_name,
                        "time": time.time(),
                    }
                )
            else:
                files_collection.insert_one(
                    {
                        "_id": file_id,
                        "filename": filename,
                        "data": file_bytes,
                        "size": len(file_bytes),
                        "uploader": uploader_name,
                        "time": time.time(),
                    }
                )

            MAX_FILES = 100
            file_count = files_collection.count_documents({})
            if file_count > MAX_FILES:
                delete_count = file_count - MAX_FILES
                old_files = (
                    files_collection.find({}, {"_id": 1})
                    .sort("time", 1)
                    .limit(delete_count)
                )
                old_ids = [f["_id"] for f in old_files]
                if old_ids:
                    files_collection.delete_many({"_id": {"$in": old_ids}})
        except Exception as e:
            print(f"存入网盘失败: {e}")

        global recent_sent_messages
        qq_notification_text = f"[网页-{location}]{username}: [发送了文件: {filename}]"

        recent_sent_messages.append(re.sub(r"\s+", "", qq_notification_text))
        if len(recent_sent_messages) > 50:
            recent_sent_messages.pop(0)

        try:
            requests.post(
                "http://127.0.0.1:3000/send_group_msg",
                json={"group_id": TARGET_GROUP_ID, "message": qq_notification_text},
                timeout=3,
            )
        except Exception as e:
            print(f"发送文件前置文字提示失败: {e}")

        local_port = os.environ.get("PORT", 7860)
        download_url = f"http://127.0.0.1:{local_port}/api/internal_download/{file_id}"

        resp = requests.post(
            "http://127.0.0.1:3000/upload_group_file",
            json={"group_id": TARGET_GROUP_ID, "file": download_url, "name": filename},
            timeout=60,
        )

        resp_data = resp.json()
        if resp_data.get("status") == "failed":
            raise Exception(
                f"机器人报错: {resp_data.get('wording', resp_data.get('msg', '未知拦截'))}"
            )

        try:
            del internal_file_cache[file_id]
        except Exception:
            pass

        global current_msg_id, chat_history
        current_msg_id += 1
        current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

        safe_user = html.escape(username)
        safe_filename = html.escape(filename)

        web_display_text = f"[网页-{location}]{safe_user}: <span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <a href='/api/download_file?id={file_id}' target='_blank' style='color: blue; text-decoration:underline;'>[下载]</a>"

        new_msg = {
            "id": current_msg_id,
            "sender_title": f"消息 {current_time_str}",
            "text": web_display_text,
            "pure_sender": username,
            "pure_text": f"[发送了文件: {filename}]",
        }

        chat_history.append(new_msg)
        save_chat_message(new_msg)

        if len(chat_history) > 50:
            chat_history.pop(0)

        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("ok", "");/* ]]> */</script>'

    except Exception as e:
        print(f"上传文件失败: {e}")
        safe_error_msg = str(e).replace('"', "").replace("'", "").replace("\n", " ")
        return f'<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "{safe_error_msg}");/* ]]> */</script>'


@app.route("/drive", methods=["GET"])
def view_drive():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()

    if page < 1:
        page = 1

    per_page = 20

    query = {}
    if q:
        query["$or"] = [
            {"filename": {"$regex": q, "$options": "i"}},
            {"uploader": {"$regex": q, "$options": "i"}},
        ]

    total_files = files_collection.count_documents(query)
    total_pages = (total_files + per_page - 1) // per_page if total_files > 0 else 1

    if page > total_pages:
        page = total_pages

    skip_count = (page - 1) * per_page

    file_docs = list(
        files_collection.find(query, {"data": 0})
        .sort("time", -1)
        .skip(skip_count)
        .limit(per_page)
    )

    display_files = []
    for f in file_docs:
        size_bytes = f.get("size", 0)
        size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else "未知"

        dt = datetime.datetime.fromtimestamp(f.get("time", 0), tz=TZ_UTC8)

        display_files.append(
            {
                "_id": f["_id"],
                "filename": f.get("filename", "未知文件"),
                "uploader": f.get("uploader", "系统"),
                "size_mb": size_mb,
                "upload_time": dt.strftime("%Y-%m-%d %H:%M"),
            }
        )

    theme = request.cookies.get("nokia_theme", "light")

    return render_template_string(
        DRIVE_HTML,
        files=display_files,
        theme=theme,
        page=page,
        total_pages=total_pages,
        q=q,
    )


@app.route("/", methods=["GET"])
def index():
    global chat_history
    account = session.get("nokia_account")

    if not account or account not in users_db:
        return redirect(url_for("login_page"))

    if users_db[account].get("status") == "pending":
        session.pop("nokia_account", None)
        return redirect(url_for("login_page"))

    recent_history = chat_history[-25:] if len(chat_history) >= 25 else chat_history
    last_id = chat_history[-1]["id"] if chat_history else 0

    saved_username = users_db[account].get("nickname", account)

    saved_username = filter_emoji(saved_username)

    online_sessions[account] = time.time()
    current_online = get_online_count()

    theme = request.cookies.get("nokia_theme", "light")
    ai_url = os.environ.get("AI_URL", "#")
    return render_template_string(
        NOKIA_HTML,
        history=recent_history,
        last_id=last_id,
        saved_username=saved_username,
        online_count=current_online,
        theme=theme,
        ai_url=ai_url,
    )


@app.route("/api/sync", methods=["POST"])
def handle_nokia_ajax():
    global chat_history, current_msg_id
    global ip_last_send_time, recent_sent_messages, global_last_send_time, ip_last_message

    data = request.get_json(silent=True) or {}
    client_last_id = data.get("last_id", 0)

    account = session.get("nokia_account")

    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return jsonify(
            {
                "messages": [
                    {
                        "sender_title": "系统",
                        "text": "登录失效或账号正在审核中，请刷新页面重新登录！",
                    }
                ],
                "last_id": client_last_id,
            }
        )

    online_sessions[account] = time.time()

    username = users_db[account].get("nickname", account)
    msg_text = data.get("message", "").strip()
    msg_text = filter_emoji(msg_text)

    client_ip = get_real_ip(request)
    intercept_msg = None

    if msg_text:
        current_time = time.time()
        last_time = ip_last_send_time.get(client_ip, 0)
        last_msg_content = ip_last_message.get(client_ip, "")

        clean_msg = re.sub(r"[^\w\u4e00-\u9fa5]", "", msg_text).lower()

        if len(msg_text) > MAX_MSG_LENGTH:
            intercept_msg = f"[系统拦截]：消息过长，最多允许 {MAX_MSG_LENGTH} 个字符。"
        elif "[CQ:" in msg_text or "&#91;CQ:" in msg_text:
            intercept_msg = "[系统拦截]：禁止发送带有特殊控制符的代码！"
        elif any(banned_word.lower() in clean_msg for banned_word in BANNED_WORDS):
            intercept_msg = "[系统拦截]：发送失败，您的消息包含违规词汇！"
        elif msg_text == last_msg_content:
            intercept_msg = "[系统拦截]：请勿连续发送完全相同的内容。"
        elif current_time - last_time < COOLDOWN_SECONDS:
            intercept_msg = (
                f"[系统拦截]：发送太快啦，请等待 {COOLDOWN_SECONDS} 秒后再试！"
            )
        elif current_time - global_last_send_time < GLOBAL_COOLDOWN:
            intercept_msg = "[系统拦截]：当前网页端发送人数过多，通道拥挤，请稍后再试。"

        if intercept_msg:
            print(
                f"🛡️ [安全拦截] IP:{client_ip} 账号:{account}({username}) 尝试发送: {msg_text} -> 原因: {intercept_msg}"
            )
        else:
            ip_last_send_time[client_ip] = current_time
            ip_last_message[client_ip] = msg_text
            global_last_send_time = current_time

            reply_match = re.search(r"^\[回复:(\d+)\]\s*", msg_text)
            reply_prefix_qq = ""
            reply_prefix_web = ""
            reply_cq_code = ""
            clean_text = msg_text

            if reply_match:
                reply_id = int(reply_match.group(1))
                clean_text = msg_text[reply_match.end() :]
                target_msg = next(
                    (m for m in chat_history if m["id"] == reply_id), None
                )
                if target_msg:
                    t_sender = target_msg.get("pure_sender", "某人")
                    t_text = target_msg.get("pure_text", "")
                    if len(t_text) > 15:
                        t_text = t_text[:15] + "..."

                    target_qq_msg_id = target_msg.get("qq_msg_id")

                    if target_qq_msg_id:
                        reply_cq_code = f"[CQ:reply,id={target_qq_msg_id}]"
                    else:
                        reply_prefix_qq = f"[回复 @{t_sender}: {t_text}]\n"

                    reply_prefix_web = f'<span class="reply-text">[回复 @{html.escape(t_sender)}: {html.escape(t_text)}]</span><br/>'

            def replace_at(match):
                name = match.group(1) or match.group(2)
                qq_uid = get_qq_by_name(name)
                if qq_uid:
                    return f"[CQ:at,qq={qq_uid}] "
                return match.group(0)

            def replace_at_for_echo(match):
                name = match.group(1) or match.group(2)
                qq_uid = get_qq_by_name(name)
                if qq_uid:
                    real_name = get_qq_name(TARGET_GROUP_ID, qq_uid)
                    return f"[@{real_name}] "
                return match.group(0)

            qq_clean_text = re.sub(
                r"\[@([^\]]+)\]|(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
                replace_at,
                clean_text,
            )
            echo_clean_text = re.sub(
                r"\[@([^\]]+)\]|(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
                replace_at_for_echo,
                clean_text,
            )

            location = get_ip_location(client_ip, request)

            full_message = f"{reply_cq_code}{reply_prefix_qq}[{location} - {username}] {qq_clean_text}"

            echo_message = (
                f"{reply_prefix_qq}[{location} - {username}] {echo_clean_text}"
            )

            recent_sent_messages.append(re.sub(r"\s+", "", echo_message))
            if len(recent_sent_messages) > 50:
                recent_sent_messages.pop(0)

            try:
                resp = requests.post(
                    "http://127.0.0.1:3000/send_group_msg",
                    json={"group_id": TARGET_GROUP_ID, "message": full_message},
                    timeout=3,
                ).json()

                qq_msg_id = None
                if resp and resp.get("status") in ["ok", "success"]:
                    qq_msg_id = resp.get("data", {}).get("message_id")

                current_msg_id += 1

                safe_user = html.escape(username)
                safe_text = html.escape(clean_text).replace("\n", "<br/>")

                safe_text = re.sub(
                    r"\[@([^\]]+)\]",
                    r'<span class="action-reply">[@\1]</span>',
                    safe_text,
                )
                safe_text = re.sub(
                    r"(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
                    r'<span class="action-reply">@\1</span>',
                    safe_text,
                )

                web_display_text = (
                    f"{reply_prefix_web}[网页-{location}]{safe_user}: {safe_text}"
                )
                current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

                new_msg = {
                    "id": current_msg_id,
                    "qq_msg_id": qq_msg_id,
                    "sender_title": f"消息 {current_time_str}",
                    "text": web_display_text,
                    "pure_sender": username,
                    "pure_text": clean_text,
                }

                chat_history.append(new_msg)
                save_chat_message(new_msg)

                if len(chat_history) > 50:
                    chat_history.pop(0)

            except Exception as e:
                print(f"发送到QQ失败: {e}")
                intercept_msg = "[系统异常]：连接 QQ 机器人后端失败。"

    new_msgs = [m for m in chat_history if m["id"] > client_last_id]
    response_msgs = []

    if intercept_msg:
        response_msgs.append({"sender_title": "系统", "text": intercept_msg})

    for m in new_msgs:
        msg_dict = {
            "id": m.get("id"),
            "sender_title": m.get("sender_title", "QQ群"),
            "text": m["text"],
            "pure_sender": m.get("pure_sender", ""),
        }
        if m.get("recall_target_id") is not None:
            msg_dict["recall_target_id"] = m["recall_target_id"]

        response_msgs.append(msg_dict)

    new_last_id = chat_history[-1]["id"] if chat_history else client_last_id

    current_online = get_online_count()

    return jsonify(
        {
            "messages": response_msgs,
            "last_id": new_last_id,
            "online_count": current_online,
        }
    )


@app.route("/webhook", methods=["POST"])
def receive_qq_msg():
    global chat_history, current_msg_id, recent_sent_messages

    client_ip = request.remote_addr or "127.0.0.1"

    if client_ip != "127.0.0.1":
        auth_header = request.headers.get("Authorization", "")
        url_token = request.args.get("token", "")
        header_token = (
            auth_header.replace("Bearer ", "").strip()
            if "Bearer " in auth_header
            else ""
        )

        if header_token != WEBHOOK_SECRET_TOKEN and url_token != WEBHOOK_SECRET_TOKEN:
            print(f"[安全拦截] 成功拦截外网伪造 Webhook 请求！黑客IP: {client_ip}")
            return jsonify({"status": "failed", "msg": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("message_type") == "group" and data.get("group_id") == TARGET_GROUP_ID:
        sender_name = data.get("sender", {}).get("nickname", "某人")

        sender_name = filter_emoji(sender_name)
        sender_name = re.sub(r"\[.*?\]", "", sender_name).strip()
        if not sender_name:
            sender_name = "无名氏"

        sender_qq = str(data.get("sender", {}).get("user_id", ""))
        if sender_qq and sender_name:
            name_to_qq_cache[sender_name] = sender_qq

        raw_msg = ""
        qq_reply_html = ""

        message_chain = data.get("message", [])
        for segment in message_chain:
            seg_type = segment.get("type")
            if seg_type == "text":
                raw_msg += segment.get("data", {}).get("text", "")
            elif seg_type == "image":
                img_url = segment.get("data", {}).get("url", "")
                if img_url:
                    img_id = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:16]

                    if not images_collection.find_one({"_id": img_id}):
                        try:
                            headers = {"User-Agent": "Mozilla/5.0"}
                            resp = requests.get(img_url, headers=headers, timeout=5)
                            if resp.status_code == 200:
                                img = Image.open(io.BytesIO(resp.content))
                                if img.mode != "RGB":
                                    img = img.convert("RGB")
                                max_width = 240
                                if img.width > max_width:
                                    ratio = max_width / img.width
                                    new_height = int(img.height * ratio)
                                    img = img.resize(
                                        (max_width, new_height),
                                        Image.Resampling.LANCZOS,
                                    )

                                img_io = io.BytesIO()
                                img.save(img_io, "JPEG", quality=70)
                                b64_data = base64.b64encode(img_io.getvalue()).decode(
                                    "utf-8"
                                )

                                images_collection.insert_one(
                                    {
                                        "_id": img_id,
                                        "data": b64_data,
                                        "time": time.time(),
                                    }
                                )

                                cleanup_old_images(500)

                        except Exception as e:
                            print(f"图床持久化或清理失败: {e}")

                    raw_msg += f"[图片] {{IMG:{img_id}}}"
                else:
                    raw_msg += "[发送了一张图片]"
            elif seg_type == "face":
                face_data = segment.get("data", {})
                face_text = face_data.get("text")
                face_id = face_data.get("id")

                if face_text:
                    clean_text = face_text.replace("[", "").replace("]", "")
                    raw_msg += f"[{clean_text}]"
                elif face_id is not None:
                    try:
                        face_name = QQ_FACES.get(int(str(face_id)))
                        if face_name:
                            raw_msg += f"[{face_name}]"
                        else:
                            raw_msg += f"[表情{face_id}]"
                    except ValueError:
                        raw_msg += "[表情]"
                else:
                    raw_msg += "[表情]"
            elif seg_type in ["bface", "mface", "marketface"]:
                raw_msg += "[动画表情]"
            elif seg_type == "record":
                raw_msg += "[发送了一条语音]"
            elif seg_type == "video":
                raw_msg += "[发送了一段视频]"

            elif seg_type == "json":
                card_data_str = segment.get("data", {}).get("data", "{}")
                prompt_text = "应用卡片"
                try:
                    inner_json = json.loads(card_data_str)
                    if isinstance(inner_json, dict) and "prompt" in inner_json:
                        prompt_text = (
                            inner_json.get("prompt", "")
                            .replace("[", "")
                            .replace("]", "")
                        )
                except:
                    pass
                if not prompt_text:
                    prompt_text = "应用卡片"
                raw_msg += f"[{prompt_text}]"
            elif seg_type == "xml":
                raw_msg += "[发送了一条图文卡片]"
            elif seg_type in ["forward", "node"]:
                raw_msg += "[发送了合并转发聊天记录]"
            elif seg_type == "share":
                title = segment.get("data", {}).get("title", "链接")
                raw_msg += f"[分享了链接: {title}]"
            elif seg_type == "music":
                title = segment.get("data", {}).get("title", "一首歌")
                raw_msg += f"[分享了音乐: {title}]"
            elif seg_type == "location":
                title = segment.get("data", {}).get("title", "某个位置")
                raw_msg += f"[分享了位置: {title}]"
            elif seg_type == "contact":
                raw_msg += "[推荐了一个联系人名片]"
            elif seg_type == "poke":
                target_qq = str(segment.get("data", {}).get("qq", ""))
                if target_qq and target_qq != "all":
                    target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
                    raw_msg += f"[戳了戳 {target_name}]"
                else:
                    raw_msg += "[戳了戳]"
            elif seg_type == "at":
                target_qq = str(segment.get("data", {}).get("qq", ""))
                if target_qq == "all":
                    raw_msg += "[@全体成员] "
                else:
                    target_name = segment.get("data", {}).get("name")
                    if not target_name:
                        target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
                    raw_msg += f"[@{target_name}] "
            elif seg_type == "reply":
                msg_id = segment.get("data", {}).get("id")
                reply_text = ""
                reply_sender = "某人"

                if msg_id:
                    try:
                        resp = requests.post(
                            "http://127.0.0.1:3000/get_msg",
                            json={"message_id": msg_id},
                            timeout=1.5,
                        ).json()
                        if resp and resp.get("status") in ["ok", "success"]:
                            orig_data = resp.get("data", {})

                            orig_qq = orig_data.get("sender", {}).get("user_id")
                            if orig_qq:
                                reply_sender = get_qq_name(TARGET_GROUP_ID, orig_qq)

                            orig_chain = orig_data.get("message", [])
                            if isinstance(orig_chain, list):
                                for orig_seg in orig_chain:
                                    if orig_seg.get("type") == "text":
                                        reply_text += orig_seg.get("data", {}).get(
                                            "text", ""
                                        )
                                    elif orig_seg.get("type") == "image":
                                        reply_text += "[图片]"
                                    elif orig_seg.get("type") == "face":
                                        reply_text += "[表情]"
                            elif isinstance(orig_chain, str):
                                reply_text = orig_chain

                            reply_text = reply_text.replace("\n", " ").replace("\r", "")
                            if len(reply_text) > 18:
                                reply_text = reply_text[:18] + "..."
                    except Exception as e:
                        print(f"提取回复内容失败: {e}")

                if reply_text:
                    qq_reply_html = f'<span class="reply-text">[回复 @{html.escape(reply_sender)}: {html.escape(reply_text)}]</span><br/>'
                else:
                    qq_reply_html = '<span class="reply-text">[回复]</span><br/>'

        raw_msg = filter_emoji(raw_msg)

        raw_msg_no_space = re.sub(r"\s+", "", raw_msg)

        if raw_msg.strip():
            if raw_msg_no_space in recent_sent_messages:
                recent_sent_messages.remove(raw_msg_no_space)
                return jsonify({"status": "ignored_echo"})

            safe_text = html.escape(raw_msg).replace("\n", "<br/>")

            safe_text = re.sub(
                r"\{IMG:([A-Za-z0-9_-]+)\}",
                r'<a href="/view_image?b64=\1" target="_blank" class="action-at">[查看]</a>',
                safe_text,
            )

            safe_text = re.sub(
                r"\[@([^\]]+)\]", r'<span class="action-reply">[@\1]</span>', safe_text
            )
            safe_text = re.sub(
                r"(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
                r'<span class="action-reply">@\1</span>',
                safe_text,
            )

            safe_sender = html.escape(sender_name)

            print(f"💬 [QQ群收到]: {safe_sender} - {raw_msg}")

            current_msg_id += 1
            current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

            qq_msg_id = data.get("message_id")

            HIDDEN_BOT_NAMES = ["WML Bot", "Bot"]
            HIDDEN_BOT_QQS = ["3979065264", "3852667296"]

            if sender_name in HIDDEN_BOT_NAMES or str(sender_qq) in HIDDEN_BOT_QQS:
                final_pure_sender = ""

                transformed_text = re.sub(
                    r"\[([^\]]+?)\s*-\s*([^\]]+?)\](?:\s|<br/>)*",
                    r"[网页-\1]\2: ",
                    safe_text,
                    count=1,
                )

                final_display_text = f"{qq_reply_html}{transformed_text}"
            else:
                final_display_text = f"{qq_reply_html}{safe_sender}: {safe_text}"
                final_pure_sender = sender_name

            new_msg = {
                "id": current_msg_id,
                "qq_msg_id": qq_msg_id,
                "sender_title": f"群聊 {current_time_str}",
                "text": final_display_text,
                "pure_sender": final_pure_sender,
                "pure_text": raw_msg,
            }

            chat_history.append(new_msg)
            save_chat_message(new_msg)

            if len(chat_history) > 50:
                chat_history.pop(0)

    elif data.get("post_type") == "notice" and data.get("group_id") == TARGET_GROUP_ID:

        event_time = data.get("time", int(time.time()))

        if event_time <= APP_START_TIME:
            return jsonify({"status": "ignored_historical_notice"})

        if time.time() - APP_START_TIME < 90:
            return jsonify({"status": "ignored_startup_burst"})

        notice_type = data.get("notice_type")
        user_qq = str(data.get("user_id", ""))

        event_fingerprint = f"{notice_type}_{user_qq}_{event_time}"
        if event_fingerprint in processed_notices_cache:
            return jsonify({"status": "ignored_cached_notice"})
        processed_notices_cache[event_fingerprint] = True

        sub_type = data.get("sub_type")
        operator_qq = str(data.get("operator_id", ""))

        sys_msg_text = ""
        recalled_qq_msg_id = None
        recalled_local_id = None

        if notice_type == "notify" and sub_type == "poke":
            sender_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            target_qq = str(data.get("target_id", ""))
            target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
            sys_msg_text = f"{sender_name} 戳了戳 {target_name}"

        elif notice_type == "group_increase":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            sys_msg_text = f"欢迎 {user_name} 加入了群聊！"
        elif notice_type == "group_decrease":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "leave":
                sys_msg_text = f"{user_name} 默默离开了群聊"
            elif sub_type in ["kick", "kick_me"]:
                operator_name = get_qq_name(TARGET_GROUP_ID, operator_qq)
                sys_msg_text = f"{user_name} 被 {operator_name} 请出了群聊"

        elif notice_type == "group_recall":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            sys_msg_text = ""

            recalled_qq_msg_id = data.get("message_id")

            if recalled_qq_msg_id:
                recalled_str = str(recalled_qq_msg_id)

                if recalled_str not in processed_notices_cache:
                    processed_notices_cache[recalled_str] = True

                    original_msg = chat_collection.find_one(
                        {"qq_msg_id": {"$in": [recalled_qq_msg_id, recalled_str]}}
                    )

                    if original_msg:
                        if original_msg.get("pure_text") != "[此消息已被撤回]":
                            sys_msg_text = f"{user_name} 撤回了一条消息"

                            for m in chat_history:
                                if str(m.get("qq_msg_id")) == recalled_str:
                                    recalled_local_id = m["id"]
                                    m["text"] = (
                                        f'<span style="color:#999; font-size:12px;"><i>[该消息已被 {html.escape(user_name)} 撤回]</i></span>'
                                    )
                                    m["pure_text"] = "[此消息已被撤回]"
                                    break

                            try:
                                chat_collection.update_many(
                                    {
                                        "qq_msg_id": {
                                            "$in": [recalled_qq_msg_id, recalled_str]
                                        }
                                    },
                                    {
                                        "$set": {
                                            "text": f'<span style="color:#999; font-size:12px;"><i>[该消息已被 {html.escape(user_name)} 撤回]</i></span>',
                                            "pure_text": "[此消息已被撤回]",
                                        }
                                    },
                                )
                            except Exception as e:
                                print(f"云端更新撤回状态失败: {e}")

        elif notice_type == "group_upload":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            file_info = data.get("file", {})
            file_name = file_info.get("name", "未知文件")

            if (
                str(user_qq) == str(data.get("self_id"))
                and file_name in recent_web_uploads
            ):
                display_name = recent_web_uploads[file_name]
                del recent_web_uploads[file_name]
            else:
                display_name = user_name

            sys_msg_text = f"{display_name} 上传了群文件: {file_name}"

            if str(user_qq) != str(data.get("self_id")):
                file_url = file_info.get("url", "")
                file_id = file_info.get("id", "")
                file_busid = file_info.get("busid", "")

                if not file_url and file_id:
                    try:
                        resp = requests.post(
                            "http://127.0.0.1:3000/get_group_file_url",
                            json={
                                "group_id": TARGET_GROUP_ID,
                                "file_id": file_id,
                                "busid": file_busid,
                            },
                            timeout=2,
                        ).json()
                        if resp and resp.get("status") in ["ok", "success"]:
                            file_url = resp.get("data", {}).get("url", "")
                    except Exception:
                        pass

                current_msg_id += 1
                current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

                safe_sender = html.escape(display_name)
                safe_filename = html.escape(file_name)

                if file_url:
                    encoded_url = urllib.parse.quote(file_url, safe="")
                    encoded_name = urllib.parse.quote(file_name)
                    proxy_url = (
                        f"/api/download_qq_file?name={encoded_name}&url={encoded_url}"
                    )
                    file_html = f"<span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <a href='{proxy_url}' target='_blank' style='color: blue; text-decoration:underline;'>[下载]</a>"
                else:
                    file_html = f"<span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <span style='color:#999; font-size:11px;'>(暂无法获取下载链接)</span>"

                new_chat_msg = {
                    "id": current_msg_id,
                    "qq_msg_id": None,
                    "sender_title": f"群聊 {current_time_str}",
                    "text": f"{safe_sender}: {file_html}",
                    "pure_sender": display_name,
                    "pure_text": f"[发送了文件: {file_name}]",
                }

                chat_history.append(new_chat_msg)
                save_chat_message(new_chat_msg)

        elif notice_type == "group_ban":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "ban":
                duration = data.get("duration", 0) // 60
                sys_msg_text = f"{user_name} 被管理员禁言了 {duration} 分钟"
            elif sub_type == "lift_ban":
                sys_msg_text = f"{user_name} 被解除了禁言"
        elif notice_type == "group_admin":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "set":
                sys_msg_text = f"{user_name} 荣升为群管理员"
            elif sub_type == "unset":
                sys_msg_text = f"{user_name} 被取消了管理员"

        if sys_msg_text:
            print(f"[系统事件]: {sys_msg_text}")

            current_msg_id += 1
            current_time_str = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")

            new_msg = {
                "id": current_msg_id,
                "sender_title": f"系统提示 {current_time_str}",
                "text": f'<span style="color:#ff6600; font-size:12px;">[通知] {html.escape(sys_msg_text)}</span>',
                "pure_sender": "系统",
                "pure_text": sys_msg_text,
            }
            if notice_type == "group_recall" and recalled_local_id is not None:
                new_msg["recall_target_id"] = recalled_local_id

            if notice_type == "group_recall" and recalled_qq_msg_id:
                new_msg["recalled_qq_msg_id"] = str(recalled_qq_msg_id)

            chat_history.append(new_msg)
            save_chat_message(new_msg)

            if len(chat_history) > 50:
                chat_history.pop(0)

    return jsonify({"status": "ok"})


@app.route("/view_image")
def view_image():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return "请先登录", 403

    img_id = request.args.get("b64")
    if not img_id:
        return "缺少图片参数", 400

    try:
        img_doc = images_collection.find_one({"_id": img_id})

        if img_doc:
            img_bytes = base64.b64decode(img_doc["data"])
            img_io = io.BytesIO(img_bytes)
            return send_file(img_io, mimetype="image/jpeg")
        else:
            if len(img_id) > 30:
                img_id += "=" * ((4 - len(img_id) % 4) % 4)
                img_url = base64.urlsafe_b64decode(img_id).decode("utf-8")

                headers = {"User-Agent": "Mozilla/5.0"}
                resp = requests.get(img_url, headers=headers, timeout=5)

                if resp.status_code != 200:
                    return (
                        f"此为旧版临时图片链接，已随时间永久失效。状态码: {resp.status_code}",
                        500,
                    )

                img = Image.open(io.BytesIO(resp.content))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                max_width = 240
                if img.width > max_width:
                    ratio = max_width / img.width
                    img = img.resize(
                        (max_width, int(img.height * ratio)), Image.Resampling.LANCZOS
                    )

                img_io = io.BytesIO()
                img.save(img_io, "JPEG", quality=70)
                img_io.seek(0)
                return send_file(img_io, mimetype="image/jpeg")

            return "图片已被清理或不存在", 404

    except Exception as e:
        print(f"图片处理异常: {e}")
        return "图片格式不受支持或源图片已失效", 500


from flask import Response, stream_with_context


@app.route("/api/download_file")
def download_web_file():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return "请先登录", 403

    file_id = request.args.get("id")
    if not file_id:
        return "缺少文件参数", 400

    try:
        file_doc = files_collection.find_one({"_id": file_id})

        if file_doc:
            filename = file_doc.get("filename", "未知文件")
            from urllib.parse import quote

            encoded_filename = quote(filename)

            if "r2_key" in file_doc and s3_client:
                r2_obj = s3_client.get_object(
                    Bucket=R2_BUCKET_NAME, Key=file_doc["r2_key"]
                )

                response_headers = {
                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": r2_obj["ContentLength"],
                }

                return Response(
                    stream_with_context(
                        r2_obj["Body"].iter_chunks(chunk_size=1024 * 512)
                    ),
                    headers=response_headers,
                )

            elif "data" in file_doc:
                file_bytes = file_doc["data"]
                file_io = io.BytesIO(file_bytes)
                response = make_response(
                    send_file(file_io, as_attachment=True, download_name=filename)
                )
                response.headers["Content-Disposition"] = (
                    f"attachment; filename*=UTF-8''{encoded_filename}"
                )
                return response
            else:
                return "文件已损坏或网盘未配置", 500
        else:
            return "文件已被清理或不存在", 404

    except Exception as e:
        print(f"文件下载异常: {e}")
        return "文件处理异常", 500


from flask import Response, stream_with_context


@app.route("/api/download_qq_file")
def download_qq_file():
    account = session.get("nokia_account")
    if (
        not account
        or account not in users_db
        or users_db[account].get("status") == "pending"
    ):
        return "请先登录", 403

    file_url = request.args.get("url")
    file_name = request.args.get("name", "未知文件")

    if not file_url:
        return "缺少文件下载链接", 400

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = requests.get(file_url, headers=headers, stream=True, timeout=10)

        if req.status_code != 200:
            return f"下载失败，腾讯服务器返回状态码: {req.status_code}", 500

        encoded_filename = urllib.parse.quote(file_name)

        response_headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Content-Type": req.headers.get("Content-Type", "application/octet-stream"),
        }

        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]

        return Response(
            stream_with_context(req.iter_content(chunk_size=1024 * 512)),
            headers=response_headers,
        )
    except Exception as e:
        print(f"中转QQ文件下载异常: {e}")
        return "文件处理异常或链接已失效", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 QQ 中转后端已启动！监听 {port} 端口...")
    app.run(host="0.0.0.0", port=port)
