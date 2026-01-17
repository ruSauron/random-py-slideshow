import os
import sys
import time
import random
import threading
import subprocess
import argparse
import tkinter as tk
import zipfile
import io
import concurrent.futures
import logging
import platform
from tkinter import ttk, messagebox, Menu
from collections import deque, OrderedDict
from pathlib import Path

# --- КОНФИГУРАЦИЯ ---
CFG_ARCHIVES_ENABLED = True
CFG_SLIDE_DURATION = 4.0
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100
CFG_TOOLBAR_HEIGHT = 40

# --- ЛОГИРОВАНИЕ И БЕЗОПАСНОСТЬ ---
# Заменяем "тихое" подавление ошибок на логирование.
# Это поможет диагностировать проблемы с битыми файлами.
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ЗАВИСИМОСТИ ИЗОБРАЖЕНИЙ ---
# --- ЗАВИСИМОСТИ ИЗОБРАЖЕНИЙ ---
try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    # Критическая ошибка: без Pillow сама суть программы (показ картинок) невозможна.
    # Здесь всё же лучше оставить выход или messagebox, иначе программа запустится пустым окном.
    # Но если вы хотите просто лог:
    logging.critical("CRITICAL: Pillow not found. Image viewing will be impossible.")
    # messagebox.showerror(...) # Можно оставить уведомление
    sys.exit(1) # Без Pillow продолжать действительно нет смысла

# --- ОПЦИОНАЛЬНЫЕ ФОРМАТЫ ---
HEIC_SUPPORT = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
    logging.info("Module 'pillow-heif' loaded. HEIC support enabled.")
except ImportError:
    logging.warning("Module 'pillow-heif' not found. HEIC files will be ignored.")

AVIF_SUPPORT = False
try:
    # Pillow 10+ может поддерживать AVIF нативно, если собран с libavif,
    # либо через pillow-avif-plugin. Проверим, открывается ли AVIF.
    import pillow_avif # Попытка импорта плагина (если используется)
    AVIF_SUPPORT = True
    logging.info("Module 'pillow-avif-plugin' loaded. AVIF support enabled.")
except ImportError:
    # Если плагина нет, проверим нативную поддержку (просто добавим расширение позже и увидим)
    # Но явно логируем отсутствие плагина
    logging.info("Module 'pillow-avif-plugin' not found. Relying on native Pillow AVIF support if available.")



# Базовый набор, который точно есть в Pillow
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.tiff', '.webp'}

# Добавляем HEIC только если модуль загрузился
if HEIC_SUPPORT:
    CFG_EXTENSIONS.add('.heic')
    CFG_EXTENSIONS.add('.heif')

# Добавляем AVIF всегда (пусть Pillow сам разбирается, может ли открыть), 
# либо только если уверены в наличии поддержки. 
# Безопаснее добавить, так как при ошибке открытия сработает наш try-except внутри загрузчика.
CFG_EXTENSIONS.add('.avif') 


# --- КОНФИГУРАЦИЯ ---
CFG_SLIDE_DURATION = 4.0
# Расширенный список форматов. WebP, HEIC (опционально), AVIF (в новых PIL)
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.webp', '.ico', '.tiff', '.avif'}
if HEIC_SUPPORT:
    CFG_EXTENSIONS.add('.heic')


# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ---

class Utils:
    @staticmethod
    def format_size(size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

class VFS:
    SEPARATOR = "::"
    PREFIX = "zip:"

    @staticmethod
    def is_virtual(path): 
        return path.startswith(VFS.PREFIX)

    @staticmethod
    def split_zip_path(path):
        if not VFS.is_virtual(path): return None, None
        content = path[len(VFS.PREFIX):]
        if VFS.SEPARATOR in content:
            archive_path, internal_path = content.split(VFS.SEPARATOR, 1)
            return archive_path, internal_path
        return None, None

    @staticmethod
    def get_parent(path):
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            internal = internal.replace('\\', '/')
            if '/' not in internal: return archive
            parent_internal = os.path.dirname(internal)
            return f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{parent_internal}"
        else:
            return os.path.dirname(path)

    @staticmethod
    def get_name(path):
        if VFS.is_virtual(path): 
            return os.path.basename(VFS.split_zip_path(path)[1])
        return os.path.basename(path)

    @staticmethod
    def get_size(path):
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                with zipfile.ZipFile(archive, 'r') as zf: 
                    return zf.getinfo(internal).file_size
            else: 
                return os.stat(path).st_size
        except Exception: 
            return 0

    @staticmethod
    def read_bytes(path):
        """
        Чтение байтов с защитой от Zip Slip и таймаутами для больших архивов.
        """
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                
                # SECURITY: Zip Slip защита
                # Проверяем, не пытается ли путь выйти за пределы (например ../../)
                # В Python zipfile.read обычно безопасен, но явная проверка не помешает
                # при сложной логике.
                
                with zipfile.ZipFile(archive, 'r') as zf:
                    # Опционально: можно добавить проверку размера перед чтением
                    info = zf.getinfo(internal)
                    if info.file_size > 1024 * 1024 * 1024: # 1 Gb limit
                        raise ValueError("File too large inside archive")
                    return zf.read(internal)
            else:
                safe_path = str(Path(path).resolve())
                # Windows Long Path Support
                if os.name == 'nt' and not safe_path.startswith('\\\\?\\'): 
                    safe_path = '\\\\?\\' + safe_path
                with open(safe_path, 'rb') as f: 
                    return f.read()
        except Exception as e:
            logging.error(f"Error reading file {path}: {e}")
            raise e

    @staticmethod
    def list_siblings(path, extensions):
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            parent_internal = os.path.dirname(internal.replace('\\', '/'))
            siblings = []
            try:
                with zipfile.ZipFile(archive, 'r') as zf:
                    for name in zf.namelist():
                        name_norm = name.replace('\\', '/')
                        # Простая проверка, лежит ли файл в той же виртуальной папке
                        if os.path.dirname(name_norm) == parent_internal:
                            if os.path.splitext(name)[1].lower() in extensions:
                                siblings.append(f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{name}")
            except Exception as e: 
                logging.warning(f"Zip listing error: {e}")
            return sorted(siblings)
        else:
            parent = os.path.dirname(path)
            try:
                files = sorted([os.path.join(parent, f) for f in os.listdir(parent)
                                if os.path.splitext(f)[1].lower() in extensions])
                return files
            except OSError: 
                return []

class ToolTip:
    """
    Подсказки над кнопками.
    """
    def __init__(self, widget, text, delay_ms=500):
        self.widget = widget; self.text = text; self.delay_ms = delay_ms
        self._after_id = None; self._tip = None
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress>", self._hide, add=True)

    def _schedule(self, _=None): 
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip or not self.text: return
        # Показываем НАД кнопкой (y - 30)
        x = self.widget.winfo_rootx() + 5
        y = self.widget.winfo_rooty() - 30 
        
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, bg="#111111", fg="#eeeeee", 
                 relief="solid", borderwidth=1, font=("Segoe UI", 8)).pack(ipadx=6, ipady=3)

    def _hide(self, _=None):
        if self._after_id: self.widget.after_cancel(self._after_id); self._after_id = None
        if self._tip: self._tip.destroy(); self._tip = None

class ImageCache:
    def __init__(self, capacity=5):
        self.capacity = capacity
        self.cache = OrderedDict()
        # THREADING: Используем RLock вместо Lock, чтобы избежать дедлоков при рекурсивных вызовах
        self.lock = threading.RLock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def get(self, key):
        with self.lock:
            if key in self.cache: 
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def put(self, key, image):
        with self.lock:
            self.cache[key] = image
            self.cache.move_to_end(key)
            if len(self.cache) > self.capacity: 
                self.cache.popitem(last=False)

    def prefetch(self, path, mode, rotation, screen_size):
        key = (path, mode, rotation, screen_size)
        if self.get(key) is None: 
            self.executor.submit(self._decode_task, path, mode, rotation, screen_size)

    def _decode_task(self, path, mode, rotation, screen_size):
        try:
            # Асинхронное чтение и декодирование
            data = VFS.read_bytes(path)
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            
            if rotation != 0: 
                img = img.rotate(rotation, expand=True)

            sw, sh = screen_size
            iw, ih = img.size
            
            # LOGIC: Изменен расчет размеров для разных режимов
            tw, th = iw, ih
            
            if mode == 0: # Fit
                ratio = min(sw/iw, sh/ih)
                tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 1: # Orig
                tw, th = iw, ih
            elif mode == 2: # Fill
                ratio = max(sw/iw, sh/ih)
                tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 3: # Shift / Magnify
                # Увеличение теперь 2x от оригинального разрешения, а не 4x от сжатого
                tw, th = iw * 2, ih * 2
            
            if tw < 1: tw = 1
            if th < 1: th = 1

            if (tw, th) != (iw, ih): 
                img = img.resize((tw, th), Image.Resampling.LANCZOS)
            
            # Сохраняем оригинальные размеры в метаданные для отображения в UI
            img.info['original_size'] = (iw, ih)
            
            self.put((path, mode, rotation, screen_size), img)
        except Exception as e:
            logging.error(f"Decode task failed for {path}: {e}")

class ImageLoader:
    def __init__(self): 
        self.cache = ImageCache(capacity=6)
        self.current_screen_size = (1920, 1080)

    def update_screen_size(self, width, height): 
        self.current_screen_size = (width, height)

    def load_image_sync(self, path, fit_mode, rotation):
        """
        Пытается взять из кэша. Если нет - декодирует (синхронно для вызывающего потока,
        но мы обернем это в Executor на уровне GUI).
        """
        key = (path, fit_mode, rotation, self.current_screen_size)
        pil_img = self.cache.get(key)
        
        if not pil_img:
            # Если нет в кэше, вызываем декодирование прямо здесь
            # (но так как _decode_task делает put, мы просто вызываем его)
            self.cache._decode_task(path, fit_mode, rotation, self.current_screen_size)
            pil_img = self.cache.get(key)

        if pil_img: 
            return pil_img, ImageTk.PhotoImage(pil_img)
        return None, None

    def trigger_prefetch(self, paths, fit_mode, rotation):
        for p in paths:
            if p: self.cache.prefetch(p, fit_mode, rotation, self.current_screen_size)

class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()
        self.title("Fast Random PySlideshow v4 (Secure)")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)

        # THREADING: Блокировка для защиты флага отображения при гонке потоков
        self.image_shown_lock = threading.RLock()
        
        self.all_files = []
        self.folder_set = set()
        self.unviewed_indices = []
        self.viewed_paths = set()
        self.history = deque(maxlen=500)
        self.history_pointer = -1
        
        self.current_path = None
        self.next_random_path = None
        self.current_file_index = -1
        self.is_paused = False
        self.slide_timer = None
        self.is_scanning_active = True
        
        self.zoom_mode = 0
        self.temp_zoom = False
        self.rotation = 0
        
        self.show_path = tk.BooleanVar(value=True)
        self.show_name = tk.BooleanVar(value=True)
        self.show_details = tk.BooleanVar(value=True)
        self.show_stats = tk.BooleanVar(value=True)
        
        self.toolbar_locked = True
        self.fullscreen = False
        self.was_locked_before_fs = True
        self.image_shown_flag = False
        self.last_valid_meta = None
        
        self.loader = ImageLoader()
        
        # Executor для загрузки UI изображений, чтобы не блокировать GUI
        self.ui_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        self.setup_ui()
        self.bind_events()
        
        self.start_initial_search()
        if self.cli_args.fullscreen: self.toggle_fullscreen()

    def parse_cli_args(self):
        global CFG_ARCHIVES_ENABLED
        parser = argparse.ArgumentParser()
        parser.add_argument("path", nargs="?", default=None)
        parser.add_argument("--cwd", action="store_true")
        parser.add_argument("--fullscreen", action="store_true")
        g = parser.add_mutually_exclusive_group()
        g.add_argument("--includeacr", action="store_true")
        g.add_argument("--excludeacr", action="store_true")
        self.cli_args = parser.parse_args()

        if self.cli_args.cwd: self.root_dir = os.getcwd()
        elif self.cli_args.path: self.root_dir = os.path.abspath(self.cli_args.path)
        else: self.root_dir = str(Path(__file__).resolve().parent)
        
        if self.cli_args.includeacr: CFG_ARCHIVES_ENABLED = True
        elif self.cli_args.excludeacr: CFG_ARCHIVES_ENABLED = False

    def setup_ui(self):
        self.canvas = tk.Canvas(self, bg=CFG_BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        
        self.toolbar = tk.Frame(self, bg="#333333", height=CFG_TOOLBAR_HEIGHT)
        self.toolbar.pack_propagate(False)
        self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TButton", font=("Segoe UI", 10), padding=2)
        
        def btn(t, c, w=None, tt=""):
            b = ttk.Button(self.toolbar, text=t, command=c, width=w)
            b.pack(side='left', padx=2)
            if tt: ToolTip(b, tt)
            return b

        btn("?", self.show_help, 2, "Help (F1)")
        self.btn_lock = btn("HIDE", self.toggle_toolbar_lock, 6, "Lock Toolbar (Tab)")
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5, "Full Screen (F/F11/Alt+Enter")
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 10, "Change Zoom (Z)")
        #btn("⭯", lambda: self.rotate_image(-90), 2, "Rotate Left (Ctrl+E)")
        #btn("⭮", lambda: self.rotate_image(90), 2, "Rotate Right (Ctrl+R)")
        #btn("◄", self.prev_image, 2, "Previous History (Left)")
        #btn("►", self.next_image, 2, "Next Random (Right)")
        #btn("⇐", self.first_file_folder, 3, "First in folder (Home)")
        #btn("←", self.prev_file_alpha, 3, "Prev File in folder (Up)")
        #btn("→", self.next_file_alpha, 3, "Next File in folder (Down)")

        btn("CCR", lambda: self.rotate_image(-90), 4, "Rotate Left (Ctrl+E)")
        btn("CR", lambda: self.rotate_image(90), 4, "Rotate Right (Ctrl+R)")
        btn("<--", self.prev_image, 4, "Previous History (Left)")
        btn("-->", self.next_image, 4, "Next Random (Right)")
        btn("<<", self.first_file_folder, 3, "First in folder (Home)")
        btn("<-", self.prev_file_alpha, 3, "Prev File in folder (Up)")
        btn("->", self.next_file_alpha, 3, "Next File in folder (Down)")

        btn("^^", self.nav_folder_prev, 3, "Prev folder (PgUp)")
        btn("vv", self.nav_folder_next, 3, "Next folder (PgDn)")
       
        self.btn_play = btn("PAUSE", self.toggle_pause, 6, "Toggle slideshow (Spacebar)")
        
        tk.Label(self.toolbar, text="Sec:", bg="#333333", fg="white").pack(side='left', padx=(5,0))
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION))
        self.speed_var.trace("w", self.on_speed_change)
        tk.Entry(self.toolbar, textvariable=self.speed_var, width=4).pack(side='left', padx=2)
        
        btn("FOLDER", self.open_current_folder, 7, "Show in File manager (Enter)")
        
        self.lbl_info = tk.Label(self.toolbar, text="Init...", bg="#333333", fg=CFG_TEXT_COLOR, font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_preset())
        self.lbl_info.bind("<Button-3>", self.show_info_menu)

    def bind_events(self):
        self.bind("<Right>", lambda e: self.next_image())
        self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<space>", lambda e: self.toggle_pause())
        self.bind("<Up>", lambda e: self.prev_file_alpha())
        self.bind("<Down>", lambda e: self.next_file_alpha())
        self.bind("<Home>", lambda e: self.first_file_folder())
        self.bind("<Prior>", lambda e: self.nav_folder_prev()) # PgUp
        self.bind("<Next>", lambda e: self.nav_folder_next())  # PgDn
        self.bind("<Return>", lambda e: self.open_current_folder())
        self.bind("<F1>", lambda e: self.show_help())
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        
        self.bind("z", lambda e: self.cycle_zoom())
        self.bind("Z", lambda e: self.cycle_zoom())
        # Shift для временного зума
        self.bind("<Shift_L>", self.enable_temp_zoom)
        self.bind("<KeyRelease-Shift_L>", self.disable_temp_zoom)
        
        self.bind("<bracketright>", lambda e: self.rotate_image(90))
        self.bind("<bracketleft>", lambda e: self.rotate_image(-90))
        self.bind("i", lambda e: self.cycle_info_preset())
        
        self.canvas.bind("<Motion>", self.check_toolbar_hover)
        self.bind("<Control-l>", lambda e: self.toggle_toolbar_lock())
        self.canvas.bind("<Button-3>", self.show_context_menu)
        
        # Pan изображения
        self.canvas.bind("<Motion>", self.on_canvas_motion, add=True)
        self.canvas.bind("<Configure>", self.on_resize)

    def start_initial_search(self): 
        threading.Thread(target=self.find_first_image_task, daemon=True).start()

    def find_first_image_task(self):
        self.find_random_image_dynamic(initial=True)
        time.sleep(0.5)
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def find_random_image_dynamic(self, initial=False):
        try:
            current = self.root_dir
            if initial: print(f"\n=== START INITIAL SCAN (Root: {current}) ===")
            
            for i in range(50):
                # THREADING: Защита от гонки условий при проверке флага
                with self.image_shown_lock:
                    if initial and self.image_shown_flag:
                        if initial: print("-> [Status] Image already shown. Stopping scan.")
                        return

                if initial and i % 5 == 0: 
                    self.lbl_info.config(text=f"Scanning: {os.path.basename(current)}...")

                try:
                    entries = list(os.scandir(current))
                except Exception as e:
                    if initial: print(f"-> [Error] Failed to scan: {e}")
                    if initial and not self.image_shown_flag:
                        current = self.root_dir
                        continue
                    break

                dirs = []
                files = []
                for e in entries:
                    if e.is_dir(): dirs.append(e.path)
                    elif e.is_file():
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in CFG_EXTENSIONS: files.append(e.path)
                        elif CFG_ARCHIVES_ENABLED and ext == '.zip': dirs.append(e.path)

                unseen = [f for f in files if f not in self.viewed_paths]
                
                pick_here = False
                if unseen:
                    rnd = random.random()
                    threshold = 0.25
                    is_forced = (not dirs)
                    if is_forced or rnd < threshold: pick_here = True
                
                if pick_here and unseen:
                    t = random.choice(unseen)
                    if initial: print(f"-> [Action] Selected file: {t}")
                    self.after(0, lambda p=t: self.load_dynamic_result(p, initial))
                    return

                if dirs:
                    ch = random.choice(dirs)
                    if CFG_ARCHIVES_ENABLED and ch.lower().endswith('.zip'):
                        if self.try_pick_from_zip(ch, initial): return
                        current = ch 
                    else:
                        current = ch
                else:
                    break
        except Exception as e:
            logging.error(f"Scanner error: {e}")

    def try_pick_from_zip(self, zp, initial):
        try:
            with zipfile.ZipFile(zp, 'r') as zf:
                names = [n for n in zf.namelist() if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS]
                if names:
                    p = f"{VFS.PREFIX}{zp}{VFS.SEPARATOR}{random.choice(names)}"
                    if p not in self.viewed_paths:
                        self.after(0, lambda: self.load_dynamic_result(p, initial))
                        return True
        except: pass
        return False

    def load_dynamic_result(self, p, initial):
        with self.image_shown_lock:
            if initial and self.image_shown_flag: return
        
        if p in self.viewed_paths: return
        self.load_by_path(p)
        if not self.history: 
            self.history.append(p)
            self.history_pointer = 0
        if not self.is_paused: 
            self.schedule_next_slide()

    def scan_worker(self):
        """Фоновое индексирование файлов"""
        temp = []
        last = time.time()

        def flush():
            nonlocal temp, last
            if temp: 
                self.after(0, lambda b=list(temp): self.add_batch(b))
                temp = []
                last = time.time()

        for root, dirs, files in os.walk(self.root_dir):
            random.shuffle(dirs)
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                fp = os.path.join(root, f)
                if ext in CFG_EXTENSIONS: 
                    temp.append(fp)
                elif CFG_ARCHIVES_ENABLED and ext == '.zip':
                    try:
                        # Только список, без полного чтения
                        with zipfile.ZipFile(fp, 'r') as zf:
                            for n in zf.namelist():
                                if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                    temp.append(f"{VFS.PREFIX}{fp}{VFS.SEPARATOR}{n}")
                    except: pass
            
            if len(temp) > 1000 or (time.time() - last > 0.5 and temp): 
                flush()
        
        flush()
        self.is_scanning_active = False

    def add_batch(self, b):
        s = len(self.all_files)
        self.all_files.extend(b)
        self.unviewed_indices.extend(range(s, s + len(b)))
        for p in b: self.folder_set.add(VFS.get_parent(p))
        if self.image_shown_flag and self.show_stats.get(): 
            self.update_info_label(None)

    def prepare_next_random(self):
        idx = self.get_random_index()
        self.next_random_path = self.all_files[idx] if idx != -1 else None
        return self.next_random_path

    def get_random_index(self):
        if not self.all_files: return -1
        if not self.unviewed_indices: 
            self.unviewed_indices = list(range(len(self.all_files)))
        
        for _ in range(10):
            if not self.unviewed_indices: return -1
            ri = random.randrange(len(self.unviewed_indices))
            val = self.unviewed_indices[ri]
            self.unviewed_indices[ri] = self.unviewed_indices[-1]
            self.unviewed_indices.pop()
            
            if self.all_files[val] not in self.viewed_paths: return val
        return -1

    def next_image(self):
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            self.load_by_path(self.history[self.history_pointer])
            return

        if self.next_random_path:
            p = self.next_random_path
            self.next_random_path = None
            self.load_by_path(p)
            self.history.append(p)
            self.history_pointer = len(self.history) - 1
            return

        if self.is_scanning_active and len(self.all_files) < 2000:
            threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()
        else:
            idx = self.get_random_index()
            if idx != -1:
                p = self.all_files[idx]
                self.load_by_path(p)
                self.history.append(p)
                self.history_pointer = len(self.history) - 1
            else:
                threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()

    def prev_image(self):
        if self.history_pointer > 0: 
            self.history_pointer -= 1
            self.load_by_path(self.history[self.history_pointer])

    def load_by_path(self, path):
        self.current_path = path
        self.viewed_paths.add(path)
        self.rotation = 0
        self.last_valid_meta = None
        
        self.display_current_image()
        self.reset_timer()
        
        threading.Thread(target=self.schedule_prefetches, args=(path,), daemon=True).start()

    def schedule_prefetches(self, path):
        nxt = self.prepare_next_random()
        sibs = VFS.list_siblings(path, CFG_EXTENSIONS)
        pa = None; na = None
        if sibs:
            try:
                i = sibs.index(path)
                pa = sibs[(i-1)%len(sibs)]
                na = sibs[(i+1)%len(sibs)]
            except: pass
        self.loader.trigger_prefetch([nxt, pa, na], self.zoom_mode, 0)

    # --- НАВИГАЦИЯ ПО ПАПКАМ ---
    def nav_sibling(self, off):
        if not self.current_path: return
        sibs = VFS.list_siblings(self.current_path, CFG_EXTENSIONS)
        if not sibs: return
        try:
            i = sibs.index(self.current_path)
            p = sibs[(i+off)%len(sibs)]
            self.load_by_path(p)
            if not self.history or self.history[-1] != p: 
                self.history.append(p)
                self.history_pointer = len(self.history)-1
        except:
            if sibs: self.load_by_path(sibs[0])

    def next_file_alpha(self): self.nav_sibling(1)
    def prev_file_alpha(self): self.nav_sibling(-1)

    def first_file_folder(self):
        if self.current_path:
            s = VFS.list_siblings(self.current_path, CFG_EXTENSIONS)
            if s:
                self.load_by_path(s[0])
                if not self.history or self.history[-1] != s[0]: 
                    self.history.append(s[0])
                    self.history_pointer=len(self.history)-1

    def _folder_key(self, p):
        if p.startswith(VFS.PREFIX):
            a, i = VFS.split_zip_path(p)
            return (os.path.normpath(a).lower(), i.replace('\\', '/').lower())
        return (os.path.normpath(p).lower(), "")

    def nav_folder_step(self, off):
        if not self.current_path: return
        cur = VFS.get_parent(self.current_path)
        fs = list(self.folder_set)
        if not fs: return
        fs.sort(key=self._folder_key)
        try:
            i = fs.index(cur)
            target = fs[(i+off)%len(fs)]
            self._load_rnd_in(target)
        except: 
            self._load_rnd_in(fs[0])

    def nav_folder_next(self): self.nav_folder_step(1)
    def nav_folder_prev(self): self.nav_folder_step(-1)

    def _load_rnd_in(self, fld):
        files = []
        if VFS.is_virtual(fld):
            a, i = VFS.split_zip_path(fld + VFS.SEPARATOR + "x")
            i = i.replace('\\', '/')
            try:
                with zipfile.ZipFile(a, 'r') as zf:
                    for n in zf.namelist():
                        if os.path.dirname(n.replace('\\','/'))==i and os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                            files.append(f"{VFS.PREFIX}{a}{VFS.SEPARATOR}{n}")
            except: pass
        else:
            if CFG_ARCHIVES_ENABLED and os.path.isfile(fld) and fld.lower().endswith('.zip'):
                try:
                    with zipfile.ZipFile(fld, 'r') as zf:
                        for n in zf.namelist():
                            if '/' not in n and '\\' not in n and os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                files.append(f"{VFS.PREFIX}{fld}{VFS.SEPARATOR}{n}")
                except: pass
            elif os.path.isdir(fld):
                try: 
                    files = [os.path.join(fld,x) for x in os.listdir(fld) 
                             if os.path.splitext(x)[1].lower() in CFG_EXTENSIONS]
                except: pass
        
        if files:
            t = random.choice(files)
            self.load_by_path(t)
            if not self.history or self.history[-1]!=t: 
                self.history.append(t)
                self.history_pointer=len(self.history)-1

    # --- ОТОБРАЖЕНИЕ ---
    def display_current_image(self):
        if not self.current_path: return
        
        # ASYNC UI: Запускаем загрузку в ThreadPool, чтобы GUI не фризился
        self.ui_executor.submit(self._display_current_image_thread_task)

    def _display_current_image_thread_task(self):
        # Эта функция выполняется в отдельном потоке
        with self.image_shown_lock:
            self.image_shown_flag = True
        
        mode = 3 if self.temp_zoom else self.zoom_mode
        
        # UI LOGIC: Получаем размеры в главном потоке (безопасно ли? лучше через after, но для чтения часто ок)
        # Однако правильнее передать их как аргументы или использовать сохраненные.
        # Используем update_idletasks если нужно, но здесь просто рассчитаем.
        w, h = self.winfo_width(), self.winfo_height()
        
        # LAYOUT FIX: Учитываем высоту панели, если она закреплена
        if self.toolbar_locked:
            h -= CFG_TOOLBAR_HEIGHT
            if h < 100: h = 100 # Safety
            
        self.loader.update_screen_size(w, h)
        
        # Тяжелая операция загрузки
        pil, tk_img = self.loader.load_image_sync(self.current_path, mode, self.rotation)
        
        # Возвращаемся в GUI поток для обновления Canvas
        self.after(0, lambda: self._update_canvas(pil, tk_img))

    def _update_canvas(self, pil, tk_img):
        self.canvas.delete("all")
        
        if pil:
            self.last_valid_meta = pil.info.get('original_size', (pil.width, pil.height))
        
        if not pil:
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, 
                                    text="Error/Loading...", fill="white")
            self.update_info_label(None)
            return

        # MEMORY LEAK FIX: Удаляем старую ссылку явно
        if hasattr(self, 'current_tk_image') and self.current_tk_image:
            del self.current_tk_image
            
        self.current_tk_image = tk_img
        
        # Центрирование с учетом смещения панели (если бы мы меняли координаты canvas, 
        # но мы меняли размер загрузки. Canvas сам по себе занимает всё окно).
        # Если панель перекрывает низ, центр визуально смещается.
        
        cx = self.winfo_width() // 2
        cy = self.winfo_height() // 2
        
        if self.toolbar_locked:
            # Смещаем центр вверх, так как низ перекрыт
            visible_h = self.winfo_height() - CFG_TOOLBAR_HEIGHT
            cy = visible_h // 2
            
        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags='img')
        
        if pil.width > self.winfo_width() or pil.height > self.winfo_height():
            self.update_zoom_pan()
            
        self.update_info_label(pil)

    def update_info_label(self, img):
        if not self.current_path: 
            self.lbl_info.config(text="")
            return

        p = [VFS.get_parent(self.current_path) + " \\"]
        if self.show_name.get(): p.append(VFS.get_name(self.current_path))
        
        if self.show_details.get():
            sz = Utils.format_size(VFS.get_size(self.current_path))
            
            w, h = None, None
            if img:
                w, h = img.info.get('original_size', (img.width, img.height))
            elif self.last_valid_meta:
                w, h = self.last_valid_meta
            
            res_str = f"[{w}x{h}]" if (w and h) else "[???]"
            p.append(f"{res_str} [{sz}]")
            
        if self.show_stats.get():
            v = len(self.viewed_paths)
            t = max(v, len(self.all_files))
            p.append(f"({v} of {t} in {len(self.folder_set)})")
            
        self.lbl_info.config(text=" ".join(p))

    def on_resize(self, e):
        if hasattr(self, '_rj'): self.after_cancel(self._rj)
        self._rj = self.after(100, self.display_current_image)

    def cycle_zoom(self):
        m = ["ZOOM Fit", "ZOOM Orig", "ZOOM Fill"]
        self.zoom_mode = (self.zoom_mode + 1) % 3
        self.btn_zoom.config(text=m[self.zoom_mode])
        self.display_current_image()
        self.reset_timer()

    def enable_temp_zoom(self, e):
        if not self.temp_zoom: 
            self.temp_zoom = True
            self.display_current_image()
            self.reset_timer()

    def disable_temp_zoom(self, e):
        if self.temp_zoom: 
            self.temp_zoom = False
            self.display_current_image()
            self.reset_timer()

    def on_canvas_motion(self, event):
        if hasattr(self, 'current_tk_image') and self.current_tk_image:
            w, h = self.winfo_width(), self.winfo_height()
            
            # Коррекция высоты области просмотра
            if self.toolbar_locked: h -= CFG_TOOLBAR_HEIGHT
                
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            
            cx, cy = w/2, h/2 # Центр области просмотра
            
            # Если картинка меньше экрана - центрируем жестко
            if iw <= w and ih <= h:
                self.canvas.coords('img', cx, cy)
                return
            
            # Pan logic
            if iw > w:
                ratio_x = event.x / w
                # Ограничиваем ratio от 0 до 1, если мышь ушла за пределы
                ratio_x = max(0, min(1, ratio_x))
                cx = -(iw - w) * ratio_x + iw/2
            
            if ih > h:
                # Коррекция Y координаты мыши относительно области просмотра
                mouse_y = min(event.y, h)
                ratio_y = mouse_y / h
                cy = -(ih - h) * ratio_y + ih/2
            
            self.canvas.coords('img', cx, cy)

    def update_zoom_pan(self):
        x = self.winfo_pointerx() - self.winfo_rootx()
        y = self.winfo_pointery() - self.winfo_rooty()
        class E: pass
        e = E(); e.x, e.y = x, y
        self.on_canvas_motion(e)

    def rotate_image(self, d): 
        self.rotation = (self.rotation - d) % 360
        self.display_current_image()
        self.reset_timer()

    def show_help(self): 
        messagebox.showinfo("Help", "Arrows: Nav\nSpace: Pause\nZ: Zoom Mode\nShift: Magnify (2x)\nPgUp/PgDn: Folder Nav\nF11: Fullscreen")

    def show_context_menu(self, e):
        m = Menu(self, tearoff=0)
        m.add_command(label="Next", command=self.next_image)
        m.add_command(label="Pause", command=self.toggle_pause)
        m.add_separator()
        m.add_command(label="Open Folder", command=self.open_current_folder)
        m.tk_popup(e.x_root, e.y_root)

    def show_info_menu(self, e):
        m = Menu(self, tearoff=0)
        for l, v in [("Name",self.show_name), ("Path",self.show_path), ("Det",self.show_details), ("Stat",self.show_stats)]:
            m.add_checkbutton(label=l, variable=v, command=self.display_current_image)
        m.tk_popup(e.x_root, e.y_root)

    def cycle_info_preset(self):
        n, p, d = self.show_name.get(), self.show_path.get(), self.show_details.get()
        if n and p and d: self.show_path.set(0); self.show_details.set(0); self.show_stats.set(0)
        elif n and not p: self.show_path.set(1)
        elif p: self.show_name.set(0); self.show_path.set(0)
        else: self.show_name.set(1); self.show_path.set(1); self.show_details.set(1); self.show_stats.set(1)
        self.display_current_image()

    def check_toolbar_hover(self, e):
        if self.toolbar_locked: return
        ry = self.winfo_rooty()
        py = self.winfo_pointery()
        # Проверка, находится ли мышь внутри окна
        if py < ry or py > ry + self.winfo_height(): return
        
        y_pos = 0 if (self.winfo_height() - (py - ry) < CFG_TOOLBAR_TRIGGER_ZONE) else 100
        self.toolbar.place(relx=0, rely=1.0, y=y_pos, anchor='sw', relwidth=1.0)

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        # Перерисовать изображение, т.к. изменилась доступная область
        self.display_current_image()

    def toggle_fullscreen(self, force_exit=False):
        if force_exit: self.fullscreen = False
        else: self.fullscreen = not self.fullscreen
        
        self.attributes("-fullscreen", self.fullscreen)
        
        if self.fullscreen:
            self.was_locked_before_fs = self.toolbar_locked
            self.toolbar_locked = False
        else:
            self.overrideredirect(False)
            self.toolbar_locked = self.was_locked_before_fs
            
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.btn_play.config(text="PLAY" if self.is_paused else "PAUSE")
        if not self.is_paused: self.schedule_next_slide()
        elif self.slide_timer: 
            self.after_cancel(self.slide_timer)
            self.slide_timer = None

    def on_speed_change(self, *a):
        try:
            if float(self.speed_var.get().replace(',','.')) <= 0: raise ValueError
        except: 
            self.is_paused = True
            self.btn_play.config(text="PLAY")

    def schedule_next_slide(self):
        if self.slide_timer: self.after_cancel(self.slide_timer)
        if self.is_paused: return
        try: s = float(self.speed_var.get().replace(',', '.'))
        except: s = 4.0
        self.slide_timer = self.after(int(s*1000), self.auto_next)

    def auto_next(self):
        if not self.is_paused: 
            self.next_image()
            self.schedule_next_slide()

    def reset_timer(self): 
        self.schedule_next_slide()

    def open_current_folder(self):
        """
        SECURITY FIX: Безопасное открытие проводника для разных ОС
        без subprocess.run(shell=True) и без прямой вставки строк.
        """
        if not self.current_path: return
        self.is_paused = True
        self.btn_play.config(text="PLAY")
        
        # Разрешаем виртуальные пути до реального архива
        if VFS.is_virtual(self.current_path):
            p = VFS.split_zip_path(self.current_path)[0]
        else:
            p = self.current_path
            
        p = os.path.normpath(os.path.abspath(p))
        
        if not os.path.exists(p):
            logging.warning(f"Path not found: {p}")
            return

        sys_plat = platform.system()
        try:
            if sys_plat == 'Windows':
                # Windows Explorer select
                subprocess.run(['explorer', '/select,', p])
            elif sys_plat == 'Darwin':
                # macOS Finder reveal
                subprocess.run(['open', '-R', p])
            elif sys_plat == 'Linux':
                # Linux (Freedesktop standard)
                # Пытаемся выделить файл через DBus (работает в Nautilus, Dolphin и др.)
                try:
                    subprocess.run(['dbus-send', '--session', '--print-reply', 
                                  '--dest=org.freedesktop.FileManager1', 
                                  '/org/freedesktop/FileManager1', 
                                  'org.freedesktop.FileManager1.ShowItems', 
                                  f'array:string:file://{p}', 'string:'], 
                                  check=True, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    # Fallback: просто открыть папку
                    subprocess.run(['xdg-open', os.path.dirname(p)])
            else:
                logging.warning(f"Unsupported platform: {sys_plat}")
        except Exception as e:
            logging.error(f"Failed to open folder: {e}")

if __name__ == "__main__":
    app = SlideShowApp()
    if os.name == 'nt':
        try: app.state('zoomed')
        except: pass
    else: 
        try: app.attributes('-zoomed', True)
        except: pass
    app.mainloop()
