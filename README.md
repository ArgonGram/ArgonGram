import base64
from datetime import datetime
import hashlib
import json
import os
import random
import socket
import threading
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen, ScreenManager, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

Window.clearcolor = (0.09, 0.13, 0.18, 1)
Window.softinput_mode = "below_target"

DB_FILE = "argongram_data.json"
LOCAL_P2P_PORT = 5555

# Кластер публичных облачных узлов (если один заблокирован провайдером, берется следующий)
CLOUD_BROKERS = [
    ("broker.hivemq.com", 1883),
    ("test.mosquitto.org", 1883),
    ("broker.emqx.io", 1883),
]


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


# --- КРИПТОГРАФИЧЕСКИЙ ПРОТОКОЛ ARGON-PROTO (SHA-256) ---
def encrypt_packet(plain_text, secret_key):
    key_bytes = hashlib.sha256(secret_key.encode("utf-8")).digest()
    data_bytes = plain_text.encode("utf-8")
    encrypted = bytes(
        [b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data_bytes)]
    )
    payload = base64.b64encode(encrypted).decode("utf-8")
    signature = hashlib.sha256(
        (payload + secret_key).encode("utf-8")
    ).hexdigest()[:12]
    return json.dumps({"payload": payload, "sig": signature})


def decrypt_packet(raw_json, secret_key):
    try:
        packet = json.loads(raw_json)
        payload = packet["payload"]
        sig = packet["sig"]
        expected_sig = hashlib.sha256(
            (payload + secret_key).encode("utf-8")
        ).hexdigest()[:12]
        if sig != expected_sig:
            return None
        encrypted = base64.b64decode(payload.encode("utf-8"))
        key_bytes = hashlib.sha256(secret_key.encode("utf-8")).digest()
        decrypted = bytes(
            [
                b ^ key_bytes[i % len(key_bytes)]
                for i, b in enumerate(encrypted)
            ]
        )
        return decrypted.decode("utf-8")
    except Exception:
        return None


# --- ПАКЕТЫ ОБЛАЧНОГО ПРОТОКОЛА ---
def _encode_varlen(length):
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 128
        out.append(byte)
        if length == 0:
            break
    return out


def build_mqtt_connect(client_id):
    cid = client_id.encode("utf-8")
    var_hdr = b"\x00\x04MQTT\x04\x02\x00\x3c"
    payload = len(cid).to_bytes(2, "big") + cid
    return (
        b"\x10"
        + _encode_varlen(len(var_hdr) + len(payload))
        + var_hdr
        + payload
    )


def build_mqtt_subscribe(topic):
    t_bytes = topic.encode("utf-8")
    var_hdr = b"\x00\x01"
    payload = len(t_bytes).to_bytes(2, "big") + t_bytes + b"\x00"
    return (
        b"\x82"
        + _encode_varlen(len(var_hdr) + len(payload))
        + var_hdr
        + payload
    )


def build_mqtt_publish(topic, message):
    t_bytes = topic.encode("utf-8")
    m_bytes = message.encode("utf-8")
    pkt = len(t_bytes).to_bytes(2, "big") + t_bytes + m_bytes
    return b"\x30" + _encode_varlen(len(pkt)) + pkt


# --- ИСПРАВЛЕННАЯ КАРТОЧКА ЧАТА (ТЕКСТ НЕ ОБРЕЗАЕТСЯ) ---
class ChatCard(BoxLayout):

    def __init__(self, room_data, on_open_callback, on_delete_callback, **kwargs):
        super().__init__(
            orientation="horizontal",
            size_hint_y=None,
            height=dp(76),
            padding=[dp(10), dp(10), dp(10), dp(10)],
            spacing=dp(10),
            **kwargs,
        )
        self.room_data = room_data

        with self.canvas.before:
            Color(0.13, 0.18, 0.24, 1)
            self.rect = RoundedRectangle(
                pos=self.pos, size=self.size, radius=[dp(12)]
            )
        self.bind(
            pos=lambda i, v: setattr(self.rect, "pos", v),
            size=lambda i, v: setattr(self.rect, "size", v),
        )

        # Цветная аватарка с первыми двумя буквами
        avatar = Button(
            text=room_data["title"][:2].upper(),
            size_hint=(None, None),
            size=(dp(54), dp(54)),
            background_normal="",
            background_color=room_data.get("color", [0.2, 0.55, 0.85, 1]),
            bold=True,
            font_size="17sp",
        )
        avatar.bind(on_press=lambda inst: on_open_callback(self.room_data))

        # Центральный блок из 2 четких строк (Заголовок + Превью)
        text_box = BoxLayout(orientation="vertical", spacing=dp(2))

        title_lbl = Label(
            text=(
                f"[b][color=ffffff]{room_data['title']}[/color][/b] "
                f"[color=ffab40][size=12sp]({room_data['key']})[/size][/color]"
            ),
            markup=True,
            halign="left",
            valign="bottom",
            font_size="15sp",
        )
        title_lbl.bind(size=lambda i, v: setattr(i, "text_size", v))

        history = room_data.get("history", [])
        last_msg = (
            f"{history[-1]['sender']}: {history[-1]['text']}"
            if history
            else "Нет сообщений. Нажмите ВХОД >>"
        )
        if len(last_msg) > 30:
            last_msg = last_msg[:30] + "..."

        sub_lbl = Label(
            text=f"[color=8899aa]{last_msg}[/color]",
            markup=True,
            halign="left",
            valign="top",
            font_size="13sp",
        )
        sub_lbl.bind(size=lambda i, v: setattr(i, "text_size", v))

        text_box.add_widget(title_lbl)
        text_box.add_widget(sub_lbl)

        self.add_widget(avatar)
        self.add_widget(text_box)

        # Кнопка удаления для пользовательских комнат
        if room_data["id"] not in ("saved_notes", "argon_global_1", "secret_bunker"):
            del_btn = Button(
                text="X",
                size_hint=(None, 1),
                width=dp(34),
                background_normal="",
                background_color=(0.45, 0.2, 0.25, 1),
                bold=True,
                font_size="12sp",
            )
            del_btn.bind(on_press=lambda inst: on_delete_callback(self.room_data))
            self.add_widget(del_btn)

        open_btn = Button(
            text="ВХОД >>",
            size_hint=(None, 1),
            width=dp(78),
            background_normal="",
            background_color=(0.18, 0.62, 0.42, 1),
            bold=True,
            font_size="12sp",
        )
        open_btn.bind(on_press=lambda inst: on_open_callback(self.room_data))
        self.add_widget(open_btn)


# --- ВИДЖЕТ ОБЛАЧКА СООБЩЕНИЯ ---
class MessageBubble(BoxLayout):

    def __init__(self, sender, text, time_str, is_mine=True, ttl=0, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, **kwargs)
        self.padding = [dp(8), dp(4), dp(8), dp(4)]
        self.sender = sender
        self.msg_text = text
        self.time_str = time_str
        self.is_mine = is_mine
        self.remaining = ttl

        bg_color = (
            (0.17, 0.42, 0.69, 1) if is_mine else (0.15, 0.21, 0.28, 1)
        )
        self.name_color = "82b1ff" if is_mine else "00e676"

        self.bubble_box = BoxLayout(
            orientation="vertical",
            size_hint_x=0.76,
            size_hint_y=None,
            padding=[dp(14), dp(10), dp(14), dp(10)],
        )

        with self.bubble_box.canvas.before:
            Color(*bg_color)
            self.rect = RoundedRectangle(
                pos=self.bubble_box.pos,
                size=self.bubble_box.size,
                radius=(
                    [dp(16), dp(16), dp(4), dp(16)]
                    if is_mine
                    else [dp(16), dp(16), dp(16), dp(4)]
                ),
            )

        self.bubble_box.bind(
            pos=lambda inst, val: setattr(self.rect, "pos", val),
            size=lambda inst, val: setattr(self.rect, "size", val),
        )

        self.lbl = Label(
            markup=True,
            size_hint_y=None,
            halign="left",
            valign="top",
            font_size="15sp",
            line_height=1.15,
            color=(0.96, 0.98, 1, 1),
        )
        self.update_label_text()

        self.lbl.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)),
            texture_size=self._sync_height,
        )

        self.bubble_box.add_widget(self.lbl)

        if is_mine:
            self.add_widget(Label(size_hint_x=0.24))
            self.add_widget(self.bubble_box)
        else:
            self.add_widget(self.bubble_box)
            self.add_widget(Label(size_hint_x=0.24))

        if self.remaining > 0:
            Clock.schedule_interval(self._tick, 1)

    def update_label_text(self):
        timer_tag = (
            f"  [color=ffab40][b][{self.remaining}с][/b][/color]"
            if self.remaining > 0
            else ""
        )
        status_tag = " [OK]" if self.is_mine else ""
        self.lbl.text = (
            f"[b][color={self.name_color}]{self.sender}[/color][/b]{timer_tag}\n"
            f"{self.msg_text}\n"
            f"[size=11sp][color=90a4ae]{self.time_str}{status_tag}[/color][/size]"
        )

    def _tick(self, dt):
        self.remaining -= 1
        if self.remaining > 0:
            self.update_label_text()
        else:
            return False

    def _sync_height(self, inst, ts):
        inst.height = ts[1]
        self.bubble_box.height = ts[1] + dp(20)
        self.height = self.bubble_box.height + dp(8)


# --- ГЛАВНОЕ ПРИЛОЖЕНИЕ ---
class ArgonGramApp(App):

    def build(self):
        self.title = "ArgonGram v2.1"
        self.load_database()
        self.current_room = None
        self.mqtt_sock = None
        self.mqtt_connected = False
        self.p2p_sock = None
        self.p2p_connected = False
        self.p2p_clients = []
        self.my_ip = get_local_ip()
        self.client_id = f"argon_{random.randint(10000, 99999)}"
        self.ttl_options = [0, 10, 30]
        self.ttl_index = 0

        # Запускаем встроенный локальный P2P-сервер в фоне
        threading.Thread(target=self.start_local_p2p_server, daemon=True).start()

        self.sm = ScreenManager(transition=SlideTransition(duration=0.22))

        # --- ЭКРАН 1: СПИСОК ЧАТОВ ---
        self.home_screen = Screen(name="home")
        home_root = BoxLayout(
            orientation="vertical", padding=dp(10), spacing=dp(8)
        )

        top_bar = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
        top_bar.add_widget(
            Label(
                text=f"[b][color=64b5f6]ARGON[/color]GRAM[/b] [size=11sp][color=00e676]({self.my_ip})[/color][/size]",
                markup=True,
                size_hint_x=0.58,
                font_size="16sp",
            )
        )
        self.nick_input = TextInput(
            text=self.db.get("nickname", "Влад"),
            hint_text="Твой ник",
            multiline=False,
            size_hint_x=0.42,
            background_normal="",
            background_color=(0.14, 0.19, 0.25, 1),
            foreground_color=(0.4, 0.9, 1, 1),
            font_size="14sp",
            padding=[dp(10), dp(10), dp(10), dp(6)],
        )
        self.nick_input.bind(text=self.save_nickname)
        top_bar.add_widget(self.nick_input)
        home_root.add_widget(top_bar)

        new_room_bar = BoxLayout(
            size_hint_y=None, height=dp(42), spacing=dp(6)
        )
        self.new_room_title = TextInput(
            hint_text="Имя новой комнаты...",
            multiline=False,
            size_hint_x=0.46,
            background_normal="",
            background_color=(0.14, 0.19, 0.25, 1),
            foreground_color=(1, 1, 1, 1),
            font_size="13sp",
            padding=[dp(10), dp(10), dp(10), dp(6)],
        )
        self.new_room_key = TextInput(
            text="argon2026",
            hint_text="Ключ шифра",
            multiline=False,
            size_hint_x=0.30,
            background_normal="",
            background_color=(0.14, 0.19, 0.25, 1),
            foreground_color=(1, 0.8, 0.3, 1),
            font_size="13sp",
            padding=[dp(10), dp(10), dp(10), dp(6)],
        )
        add_btn = Button(
            text="+ СОЗДАТЬ",
            size_hint_x=0.24,
            background_normal="",
            background_color=(0.2, 0.56, 0.9, 1),
            bold=True,
            font_size="12sp",
        )
        add_btn.bind(on_press=self.create_new_room)
        new_room_bar.add_widget(self.new_room_title)
        new_room_bar.add_widget(self.new_room_key)
        new_room_bar.add_widget(add_btn)
        home_root.add_widget(new_room_bar)

        self.rooms_scroll = ScrollView(size_hint=(1, 1))
        self.rooms_list = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=dp(8),
            padding=[0, dp(4), 0, dp(4)],
        )
        self.rooms_list.bind(minimum_height=self.rooms_list.setter("height"))
        self.rooms_scroll.add_widget(self.rooms_list)
        home_root.add_widget(self.rooms_scroll)

        self.home_screen.add_widget(home_root)
        self.sm.add_widget(self.home_screen)

        # --- ЭКРАН 2: ОКНО ПЕРЕПИСКИ ---
        self.chat_screen = Screen(name="chat")
        chat_root = BoxLayout(
            orientation="vertical", padding=dp(8), spacing=dp(6)
        )

        chat_header = BoxLayout(
            size_hint_y=None, height=dp(44), spacing=dp(6)
        )
        back_btn = Button(
            text="<< ЧАТЫ",
            size_hint_x=0.23,
            background_normal="",
            background_color=(0.16, 0.22, 0.29, 1),
            bold=True,
            font_size="12sp",
        )
        back_btn.bind(on_press=self.go_home)

        self.room_title_lbl = Label(
            text="[b]Комната[/b]",
            markup=True,
            size_hint_x=0.55,
            halign="center",
            valign="middle",
            font_size="14sp",
        )
        self.room_title_lbl.bind(size=lambda i, v: setattr(i, "text_size", v))

        clear_btn = Button(
            text="ОЧИСТ.",
            size_hint_x=0.22,
            background_normal="",
            background_color=(0.45, 0.2, 0.25, 1),
            bold=True,
            font_size="11sp",
        )
        clear_btn.bind(on_press=self.clear_current_chat)

        chat_header.add_widget(back_btn)
        chat_header.add_widget(self.room_title_lbl)
        chat_header.add_widget(clear_btn)
        chat_root.add_widget(chat_header)

        # Строка быстрой P2P-связи по IP внутри комнаты
        p2p_bar = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(6))
        self.peer_ip_input = TextInput(
            text=self.my_ip,
            hint_text="IP узла (для локальной сети)",
            multiline=False,
            size_hint_x=0.72,
            background_normal="",
            background_color=(0.14, 0.19, 0.25, 1),
            foreground_color=(0.8, 0.9, 1, 1),
            font_size="13sp",
            padding=[dp(10), dp(8), dp(10), dp(6)],
        )
        p2p_btn = Button(
            text="P2P УЗЕЛ",
            size_hint_x=0.28,
            background_normal="",
            background_color=(0.18, 0.55, 0.45, 1),
            bold=True,
            font_size="11sp",
        )
        p2p_btn.bind(on_press=self.connect_p2p_manual)
        p2p_bar.add_widget(self.peer_ip_input)
        p2p_bar.add_widget(p2p_btn)
        chat_root.add_widget(p2p_bar)

        self.chat_scroll = ScrollView(size_hint=(1, 1))
        self.chat_list = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=dp(4),
            padding=[dp(2), dp(6), dp(2), dp(6)],
        )
        self.chat_list.bind(minimum_height=self.chat_list.setter("height"))
        self.chat_scroll.add_widget(self.chat_list)
        chat_root.add_widget(self.chat_scroll)

        bottom_bar = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.timer_btn = Button(
            text="Таймер: OFF",
            size_hint_x=0.24,
            background_normal="",
            background_color=(0.2, 0.25, 0.32, 1),
            font_size="12sp",
            bold=True,
        )
        self.timer_btn.bind(on_press=self.toggle_timer)

        self.msg_input = TextInput(
            hint_text="Сообщение или /help...",
            multiline=False,
            size_hint_x=0.54,
            background_normal="",
            background_color=(0.14, 0.19, 0.25, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.45, 0.55, 0.65, 1),
            font_size="15sp",
            padding=[dp(12), dp(12), dp(12), dp(8)],
        )
        self.msg_input.bind(on_text_validate=self.send_message)

        send_btn = Button(
            text="ОТПР >>",
            size_hint_x=0.22,
            background_normal="",
            background_color=(0.2, 0.56, 0.9, 1),
            bold=True,
            font_size="13sp",
        )
        send_btn.bind(on_press=self.send_message)

        bottom_bar.add_widget(self.timer_btn)
        bottom_bar.add_widget(self.msg_input)
        bottom_bar.add_widget(send_btn)
        chat_root.add_widget(bottom_bar)

        self.chat_screen.add_widget(chat_root)
        self.sm.add_widget(self.chat_screen)

        self.refresh_rooms_ui()
        return self.sm

    # --- БАЗА ДАННЫХ И КАРТОЧКИ ---
    def load_database(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    self.db = json.load(f)
                    return
            except Exception:
                pass
        self.db = {
            "nickname": "Влад",
            "rooms": [
                {
                    "id": "saved_notes",
                    "title": "Избранное",
                    "key": "local",
                    "color": [0.2, 0.52, 0.9, 1],
                    "history": [
                        {
                            "sender": "Джарвис",
                            "text": "Личное хранилище. Доступны команды: /help, /roll, /calc 25*4",
                            "time": "02:25",
                            "is_mine": False,
                        }
                    ],
                },
                {
                    "id": "argon_global_1",
                    "title": "Глобальный чат #1",
                    "key": "argon2026",
                    "color": [0.18, 0.65, 0.45, 1],
                    "history": [],
                },
                {
                    "id": "secret_bunker",
                    "title": "Секретный бункер",
                    "key": "stalker77",
                    "color": [0.55, 0.3, 0.75, 1],
                    "history": [],
                },
            ],
        }
        self.save_database()

    def save_database(self):
        try:
            with open(DB_FILE, "w", encoding="utf-8") as f:
                json.dump(self.db, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def save_nickname(self, instance, value):
        self.db["nickname"] = value.strip() or "Аноним"
        self.save_database()

    def refresh_rooms_ui(self):
        self.rooms_list.clear_widgets()
        for room in self.db["rooms"]:
            card = ChatCard(room, self.open_room, self.delete_room)
            self.rooms_list.add_widget(card)

    def create_new_room(self, instance):
        title = self.new_room_title.text.strip()
        key = self.new_room_key.text.strip() or "argon2026"
        if not title:
            return
        room_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]
        colors = [
            [0.2, 0.55, 0.85, 1],
            [0.85, 0.4, 0.25, 1],
            [0.2, 0.68, 0.5, 1],
            [0.6, 0.35, 0.8, 1],
        ]
        new_room = {
            "id": room_id,
            "title": title,
            "key": key,
            "color": random.choice(colors),
            "history": [],
        }
        self.db["rooms"].insert(0, new_room)
        self.save_database()
        self.new_room_title.text = ""
        self.refresh_rooms_ui()

    def delete_room(self, room_data):
        if room_data in self.db["rooms"]:
            self.db["rooms"].remove(room_data)
            self.save_database()
            self.refresh_rooms_ui()

    # --- НАВИГАЦИЯ ---
    def open_room(self, room_data):
        self.current_room = room_data
        self.sm.transition.direction = "left"
        self.sm.current = "chat"

        self.chat_list.clear_widgets()
        self.room_title_lbl.text = (
            f"[b]{room_data['title']}[/b]\n"
            f"[size=11sp][color=8899aa]Синхронизация узлов...[/color][/size]"
        )

        for item in room_data.get("history", []):
            self.add_bubble_ui(
                item["sender"],
                item["text"],
                item["time"],
                is_mine=item.get("is_mine", True),
                ttl=0,
            )

        if room_data["id"] == "saved_notes":
            self.room_title_lbl.text = (
                f"[b]{room_data['title']}[/b]\n"
                f"[size=11sp][color=00e676]Локальное хранилище[/color][/size]"
            )
        else:
            # Подключаем и локальный P2P-узел, и облачный ретранслятор одновременно
            threading.Thread(
                target=self.connect_p2p_node,
                args=(self.peer_ip_input.text.strip() or self.my_ip,),
                daemon=True,
            ).start()
            threading.Thread(
                target=self.connect_cloud_room,
                args=(room_data["id"],),
                daemon=True,
            ).start()

    def go_home(self, instance):
        self.disconnect_all()
        self.current_room = None
        self.refresh_rooms_ui()
        self.sm.transition.direction = "right"
        self.sm.current = "home"

    def clear_current_chat(self, instance):
        if self.current_room:
            self.current_room["history"] = []
            self.save_database()
            self.chat_list.clear_widgets()
            self.add_system_note("История комнаты очищена.")

    # --- ГИБРИДНЫЙ СЕТЕВОЙ ДВИЖОК (P2P + CLOUD CLUSTER) ---
    def disconnect_all(self):
        self.mqtt_connected = False
        self.p2p_connected = False
        for s in (self.mqtt_sock, self.p2p_sock):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self.mqtt_sock = None
        self.p2p_sock = None

    def start_local_p2p_server(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", LOCAL_P2P_PORT))
            server.listen(10)
            while True:
                conn, _ = server.accept()
                self.p2p_clients.append(conn)
                threading.Thread(
                    target=self.handle_p2p_client, args=(conn,), daemon=True
                ).start()
        except Exception:
            pass

    def handle_p2p_client(self, conn):
        while True:
            try:
                data = conn.recv(4096).decode("utf-8")
                if not data:
                    break
                for c in list(self.p2p_clients):
                    if c != conn:
                        try:
                            c.send(data.encode("utf-8"))
                        except Exception:
                            c.close()
                            if c in self.p2p_clients:
                                self.p2p_clients.remove(c)
            except Exception:
                break
        if conn in self.p2p_clients:
            self.p2p_clients.remove(conn)
        conn.close()

    def connect_p2p_manual(self, instance):
        target_ip = self.peer_ip_input.text.strip()
        if target_ip:
            threading.Thread(
                target=self.connect_p2p_node, args=(target_ip,), daemon=True
            ).start()

    def connect_p2p_node(self, target_ip):
        if self.p2p_sock:
            try:
                self.p2p_connected = False
                self.p2p_sock.close()
            except Exception:
                pass
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((target_ip, LOCAL_P2P_PORT))
            sock.settimeout(None)
            self.p2p_sock = sock
            self.p2p_connected = True
            Clock.schedule_once(lambda dt: self.refresh_connection_badge())
            while self.p2p_connected:
                raw = sock.recv(4096).decode("utf-8")
                if not raw:
                    break
                Clock.schedule_once(
                    lambda dt, p=raw: self.process_incoming_packet(p)
                )
        except Exception:
            self.p2p_connected = False
            Clock.schedule_once(lambda dt: self.refresh_connection_badge())

    def connect_cloud_room(self, room_id):
        topic = f"argongram/v2/{room_id}"
        for host, port in CLOUD_BROKERS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((host, port))
                sock.sendall(build_mqtt_connect(self.client_id))
                resp = sock.recv(4)
                if not resp or resp[0] != 0x20:
                    sock.close()
                    continue
                sock.sendall(build_mqtt_subscribe(topic))
                sock.settimeout(None)
                self.mqtt_sock = sock
                self.mqtt_connected = True
                Clock.schedule_once(lambda dt: self.refresh_connection_badge())
                self.mqtt_receive_loop()
                return
            except Exception:
                continue
        self.mqtt_connected = False
        Clock.schedule_once(lambda dt: self.refresh_connection_badge())

    def refresh_connection_badge(self):
        if not self.current_room:
            return
        modes = []
        if self.mqtt_connected:
            modes.append("Облако")
        if self.p2p_connected:
            modes.append("P2P")
        if modes:
            badge = f"[color=00e676]Онлайн ({' + '.join(modes)})[/color]"
        else:
            badge = "[color=ffab40]Автономный режим[/color]"
        self.room_title_lbl.text = (
            f"[b]{self.current_room['title']}[/b]\n[size=11sp]{badge}[/size]"
        )

    def mqtt_receive_loop(self):
        while self.mqtt_connected and self.mqtt_sock:
            try:
                first = self.mqtt_sock.recv(1)
                if not first:
                    break
                pkt_type = first[0]
                multiplier = 1
                rem_len = 0
                while True:
                    b = self.mqtt_sock.recv(1)
                    if not b:
                        break
                    rem_len += (b[0] & 127) * multiplier
                    multiplier *= 128
                    if (b[0] & 128) == 0:
                        break
                data = b""
                while len(data) < rem_len:
                    chunk = self.mqtt_sock.recv(rem_len - len(data))
                    if not chunk:
                        break
                    data += chunk
                if (pkt_type & 0xF0) == 0x30 and len(data) > 2:
                    t_len = int.from_bytes(data[:2], "big")
                    raw_payload = data[2 + t_len :].decode(
                        "utf-8", errors="ignore"
                    )
                    Clock.schedule_once(
                        lambda dt, p=raw_payload: self.process_incoming_packet(
                            p
                        )
                    )
            except Exception:
                break
        self.mqtt_connected = False

    def process_incoming_packet(self, raw_payload):
        if not self.current_room:
            return
        secret_key = self.current_room["key"]
        decrypted_json = decrypt_packet(raw_payload, secret_key)

        if decrypted_json is None:
            return

        try:
            msg_obj = json.loads(decrypted_json)
            if msg_obj.get("cid") == self.client_id:
                return
            if msg_obj.get("room") != self.current_room["id"]:
                return

            sender = msg_obj["nick"]
            text = msg_obj["text"]
            time_str = msg_obj["time"]
            ttl = msg_obj.get("ttl", 0)

            self.add_bubble_ui(sender, text, time_str, is_mine=False, ttl=ttl)

            if ttl == 0:
                self.current_room["history"].append(
                    {
                        "sender": sender,
                        "text": text,
                        "time": time_str,
                        "is_mine": False,
                    }
                )
                self.save_database()
        except Exception:
            pass

    # --- ОТПРАВКА И КОМАНДЫ ---
    def toggle_timer(self, instance):
        self.ttl_index = (self.ttl_index + 1) % len(self.ttl_options)
        ttl = self.ttl_options[self.ttl_index]
        if ttl == 0:
            self.timer_btn.text = "Таймер: OFF"
            self.timer_btn.background_color = (0.2, 0.25, 0.32, 1)
        else:
            self.timer_btn.text = f"Таймер: {ttl}с"
            self.timer_btn.background_color = (0.85, 0.45, 0.15, 1)

    def add_system_note(self, text):
        lbl = Label(
            text=f"[color=8899aa]{text}[/color]",
            markup=True,
            size_hint_y=None,
            height=dp(30),
            halign="center",
            font_size="12sp",
        )
        self.chat_list.add_widget(lbl)
        Clock.schedule_once(
            lambda dt: setattr(self.chat_scroll, "scroll_y", 0), 0.1
        )

    def add_bubble_ui(self, sender, text, time_str, is_mine=True, ttl=0):
        bubble = MessageBubble(sender, text, time_str, is_mine=is_mine, ttl=ttl)
        self.chat_list.add_widget(bubble)
        Clock.schedule_once(
            lambda dt: setattr(self.chat_scroll, "scroll_y", 0), 0.1
        )
        if ttl > 0:
            Clock.schedule_once(
                lambda dt: (
                    self.chat_list.remove_widget(bubble)
                    if bubble in self.chat_list.children
                    else None
                ),
                ttl,
            )

    def handle_slash_command(self, cmd_text):
        if cmd_text == "/help":
            return "Команды ArgonGram:\n/roll — кубик (1-100)\n/calc <пример> — калькулятор\n/id — данные шифрования комнаты"
        if cmd_text == "/roll":
            return f"Бросок кубика: выпало [{random.randint(1, 100)} из 100]"
        if cmd_text == "/id" and self.current_room:
            return f"Комната: {self.current_room['title']} | Ключ: {self.current_room['key']}"
        if cmd_text.startswith("/calc "):
            expr = cmd_text[6:].strip()
            try:
                allowed = set("0123456789+-*/(). ")
                if all(c in allowed for c in expr):
                    return f"Расчет: {expr} = {eval(expr)}"
                return "Разрешены только числа и знаки + - * /"
            except Exception:
                return "Ошибка в примере."
        return None

    def send_message(self, instance):
        text = self.msg_input.text.strip()
        if not text or not self.current_room:
            return

        self.msg_input.text = ""
        nick = self.db.get("nickname", "Влад")
        now = datetime.now().strftime("%H:%M")
        ttl = self.ttl_options[self.ttl_index]

        bot_reply = self.handle_slash_command(text)
        if bot_reply:
            self.add_bubble_ui(
                "Джарвис (Система)", bot_reply, now, is_mine=False, ttl=ttl
            )
            return

        self.add_bubble_ui(f"{nick} (Вы)", text, now, is_mine=True, ttl=ttl)

        if ttl == 0:
            self.current_room["history"].append(
                {
                    "sender": f"{nick} (Вы)",
                    "text": text,
                    "time": now,
                    "is_mine": True,
                }
            )
            self.save_database()

        payload_dict = {
            "cid": self.client_id,
            "room": self.current_room["id"],
            "nick": nick,
            "text": text,
            "time": now,
            "ttl": ttl,
        }
        encrypted_packet = encrypt_packet(
            json.dumps(payload_dict), self.current_room["key"]
        )

        # Отправка одновременно и в P2P, и в Облако
        if self.p2p_connected and self.p2p_sock:
            try:
                self.p2p_sock.send(encrypted_packet.encode("utf-8"))
            except Exception:
                pass

        if self.mqtt_connected and self.mqtt_sock:
            topic = f"argongram/v2/{self.current_room['id']}"
            try:
                pkt = build_mqtt_publish(topic, encrypted_packet)
                self.mqtt_sock.sendall(pkt)
            except Exception:
                pass


if __name__ == "__main__":
    ArgonGramApp().run()
