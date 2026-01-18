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
import re  # Для естественной сортировки (Natural Sort)

from tkinter import ttk, messagebox, Menu
from collections import deque, OrderedDict
from pathlib import Path

# --- КОНФИГУРАЦИЯ ---
CFG_ARCHIVES_ENABLED = True
CFG_SLIDE_DURATION = 4.0
CFG_FORCE_MIN_DURATION = True  # Если True, таймер тикает только когда картинка УЖЕ на экране
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100
CFG_TOOLBAR_HEIGHT = 40
CFG_SLIDE_MODE = "random" #random | sequential

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ЗАВИСИМОСТИ ИЗОБРАЖЕНИЙ ---
try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    logging.critical("CRITICAL: Pillow not found. Image viewing will be impossible.")
    sys.exit(1)

# --- ОПЦИОНАЛЬНЫЕ ФОРМАТЫ ---
HEIC_SUPPORT = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    pass

CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.tiff', '.webp', '.ico', '.avif'}
if HEIC_SUPPORT:
    CFG_EXTENSIONS.add('.heic')
    CFG_EXTENSIONS.add('.heif')

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ---

class Utils:
    @staticmethod
    def format_size(size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

    @staticmethod
    def natural_keys(text):
        """
        Ключ сортировки для человеческого восприятия чисел.
        Превращает 'file10.jpg' в [..., 10, ...], чтобы 10 шло после 2.
        """
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

class VFS:
    """Виртуальная файловая система для прозрачной работы с ZIP."""
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
            return content.split(VFS.SEPARATOR, 1)
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
        """Безопасное чтение файла (реального или из архива)."""
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                with zipfile.ZipFile(archive, 'r') as zf:
                    info = zf.getinfo(internal)
                    if info.file_size > 1024 * 1024 * 1024: # 1 GB limit for safety
                        raise ValueError("File too large inside archive")
                    return zf.read(internal)
            else:
                safe_path = str(Path(path).resolve())
                if os.name == 'nt' and not safe_path.startswith('\\\\?\\'):
                    safe_path = '\\\\?\\' + safe_path
                with open(safe_path, 'rb') as f:
                    return f.read()
        except Exception as e:
            logging.error(f"Error reading file {path}: {e}")
            raise e

    @staticmethod
    def list_siblings(path, extensions, sort_method=None):
        """
        Возвращает список соседних файлов в той же папке.
        Использует переданный метод сортировки (обычно Utils.natural_keys).
        """
        key_func = sort_method if sort_method else (lambda x: x.lower())

        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            parent_internal = os.path.dirname(internal.replace('\\', '/'))
            siblings = []
            try:
                with zipfile.ZipFile(archive, 'r') as zf:
                    for name in zf.namelist():
                        name_norm = name.replace('\\', '/')
                        if os.path.dirname(name_norm) == parent_internal:
                            if os.path.splitext(name)[1].lower() in extensions:
                                siblings.append(f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{name}")
            except: pass
            siblings.sort(key=key_func)
            return siblings
        else:
            parent = os.path.dirname(path)
            try:
                files = [os.path.join(parent, f) for f in os.listdir(parent)
                         if os.path.splitext(f)[1].lower() in extensions]
                files.sort(key=key_func)
                return files
            except OSError:
                return []

class ToolTip:
    """Всплывающие подсказки."""
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
    """
    Кэш с поддержкой приоритетов, поколений задач и раздельных пулов.
    """
    def __init__(self, capacity=10):
        self.capacity = capacity
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        
        # Пул для быстрых превью (2 потока, чтобы не блокировать интерфейс)
        self.executor_fast = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Пул для тяжелого декодинга (1 поток, чтобы задачи выстраивались в очередь)
        self.executor_hq = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        
        # Поколение задач. Инкрементируется при смене слайда.
        # Позволяет "отменять" старые задачи (они просто завершаются без результата).
        self.current_generation = 0
        self.gen_lock = threading.Lock()

    def cancel_all_tasks(self):
        """Инвалидирует все текущие задачи в очередях."""
        with self.gen_lock:
            self.current_generation += 1

    def get(self, key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key] # (Image, is_final)
            return None

    def put(self, key, image, is_final):
        with self.lock:
            # Если уже есть финальная версия, черновик её не перезаписывает
            if key in self.cache and self.cache[key][1] and not is_final:
                return
            self.cache[key] = (image, is_final)
            self.cache.move_to_end(key)
            if len(self.cache) > self.capacity:
                self.cache.popitem(last=False)

    def submit_load_task(self, path, mode, rotation, screen_size, is_draft, callback=None, generation=None):
        """
        Ставит задачу на декодирование.
        is_draft=True -> executor_fast
        is_draft=False -> executor_hq
        """
        if generation is None:
            with self.gen_lock: generation = self.current_generation

        key = (path, mode, rotation, screen_size)
        
        # Проверка кэша перед запуском (чтобы не делать лишнюю работу)
        cached = self.get(key)
        if cached:
            if cached[1]: return # Уже есть финал
            if not cached[1] and is_draft: return # Уже есть драфт, а мы хотим драфт

        executor = self.executor_fast if is_draft else self.executor_hq
        executor.submit(self._worker, key, is_draft, generation, callback)

    def _worker(self, key, is_draft, task_gen, callback):
        # 1. Проверка актуальности задачи
        with self.gen_lock:
            if task_gen != self.current_generation:
                return # Задача устарела, пропускаем

        path, mode, rotation, screen_size = key
        
        try:
            # Чтение
            data = VFS.read_bytes(path)
            
            # Повторная проверка перед тяжелым декодингом
            with self.gen_lock:
                if task_gen != self.current_generation: return

            img = Image.open(io.BytesIO(data))
            original_size = img.size
            iw, ih = original_size
            sw, sh = screen_size

            # --- ЭВРИСТИКА / ОПТИМИЗАЦИЯ ---
            # Применяем draft только если явно попросили (is_draft=True)
            # ИЛИ если это HQ, но картинка колоссально больше экрана (для экономии RAM/CPU)
            
            if is_draft and img.format == 'JPEG':
                 # Агрессивный драфт
                 try: img.draft(None, (iw//4, ih//4))
                 except: pass
            
            elif not is_draft and img.format == 'JPEG':
                 # Умный HQ: если картинка 8000x6000, а экран 1920x1080,
                 # нет смысла декодировать полностью. Делаем draft x2 от экрана.
                 ratio = min(sw/iw, sh/ih) if mode == 0 else max(sw/iw, sh/ih)
                 if ratio < 0.25: # Если уменьшаем более чем в 4 раза
                      target_w = max(sw, int(iw * ratio * 2.0))
                      target_h = max(sh, int(ih * ratio * 2.0))
                      try: img.draft(None, (target_w, target_h))
                      except: pass

            img = ImageOps.exif_transpose(img)
            if rotation != 0: img = img.rotate(rotation, expand=True)

            # Ресайз
            cw, ch = img.size
            tw, th = cw, ch
            
            if mode == 0:   ratio = min(sw/cw, sh/ch)
            elif mode == 1: ratio = 1.0
            elif mode == 2: ratio = max(sw/cw, sh/ch)
            elif mode == 3: ratio = 2.0
            
            if mode != 1:
                tw, th = int(cw*ratio), int(ch*ratio)
            
            if tw < 1: tw = 1; 
            if th < 1: th = 1

            if (tw, th) != (cw, ch):
                # Draft -> Bilinear (быстро), HQ -> Lanczos (качественно)
                resample = Image.Resampling.BILINEAR if is_draft else Image.Resampling.LANCZOS
                if tw < cw and th < ch:
                    img.thumbnail((tw, th), resample)
                else:
                    img = img.resize((tw, th), resample)

            img.info['original_size'] = original_size
            
            # Финальная проверка перед сохранением
            with self.gen_lock:
                if task_gen != self.current_generation: return

            self.put(key, img, not is_draft)
            
            if callback:
                callback(path, img, not is_draft)

        except Exception as e:
            logging.error(f"Worker error ({path}): {e}")

class ImageLoader:
    def __init__(self):
        self.cache = ImageCache(capacity=10)
        self.current_screen_size = (1920, 1080)

    def update_screen_size(self, width, height):
        self.current_screen_size = (width, height)

    def get_image_direct(self, path, mode, rotation):
        """Синхронная попытка получить из кэша (для мгновенного отображения)."""
        return self.cache.get((path, mode, rotation, self.current_screen_size))

    def request_load(self, path, mode, rotation, is_draft, callback=None):
        """Асинхронный запрос на загрузку."""
        self.cache.submit_load_task(
            path, mode, rotation, self.current_screen_size, is_draft, callback
        )

    def cancel_all(self):
        self.cache.cancel_all_tasks()
        self.current_screen_size = (1920, 1080)

    def update_screen_size(self, width, height):
        self.current_screen_size = (width, height)

    def get_image_async(self, path, fit_mode, rotation, on_loaded):
        """
        Запрашивает изображение.
        Возвращает (PillowImage, is_final) сразу, если есть в кэше.
        Если нет, вернет (None, False) и вызовет on_loaded позже.
        """
        return self.cache.request_image(path, fit_mode, rotation, self.current_screen_size, on_loaded)

    def trigger_prefetch(self, paths, fit_mode, rotation):
        for p in paths:
            if p: 
                # Предзагрузка всегда запрашивает Draft сначала, потом HQ
                # Callback = None, т.к. нам не нужно обновлять экран
                self.cache.request_image(p, fit_mode, rotation, self.current_screen_size, None)


# --- ГЛАВНЫЙ КЛАСС ПРИЛОЖЕНИЯ ---

class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()
        self.slide_mode = CFG_SLIDE_MODE or 'random' # Default mode
        
        title_mode = "SEQ" if self.slide_mode == 'sequential' else "RND"
        self.title(f"Fast PySlideshow ({title_mode})")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)

        # Синхронизация потоков
        self.image_shown_lock = threading.RLock()
        self.image_shown_flag = False

        # Данные
        self.all_files = []      # Главный список, ВСЕГДА отсортирован (A-Z)
        self.folder_set = set()  # Для статистики
        
        # История и навигация
        self.viewed_paths = set()
        self.history = deque(maxlen=500)
        self.history_pointer = -1
        
        self.current_path = None
        self.next_random_prepared = None # Для предзагрузки следующего случайного
        self.is_paused = False
        self.slide_timer = None
        self.is_scanning_active = True

        # Состояние UI
        self.zoom_mode = 0
        self.temp_zoom = False
        self.rotation = 0
        self.toolbar_locked = True
        self.fullscreen = False
        self.was_locked_before_fs = True
        
        # Инфо-панель
        self.show_path = tk.BooleanVar(value=True)
        self.show_name = tk.BooleanVar(value=True)
        self.show_details = tk.BooleanVar(value=True)
        self.show_stats = tk.BooleanVar(value=True)
        self.last_valid_meta = None

        # Исполнители
        self.loader = ImageLoader()
        self.ui_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        self.setup_ui()
        self.bind_events()
        self.start_initial_search()

        if self.cli_args.fullscreen: 
            self.toggle_fullscreen()

    def parse_cli_args(self):
        global CFG_ARCHIVES_ENABLED, CFG_SLIDE_MODE, CFG_SLIDE_DURATION, CFG_BG_COLOR
        parser = argparse.ArgumentParser(add_help=False) # Отключаем стандартный help
        parser.add_argument("path", nargs="?", default=None)
        parser.add_argument("--fullscreen", action="store_true")
        
        # Режимы
        shuffle_group = parser.add_mutually_exclusive_group()
        shuffle_group .add_argument("--shuffle", action="store_true")
        shuffle_group .add_argument("--sequential", "--seq", action="store_true")

        parser.add_argument("--help", "-h", "-?", action="store_true")

        archive_group = parser.add_mutually_exclusive_group()
        archive_group.add_argument("--includeacr", action="store_true")
        archive_group.add_argument("--excludeacr", action="store_true")

        parser.add_argument("--duration", "-t", type=float, help="Slide duration (sec)")
        parser.add_argument("--bg", type=str, help="Background color (hex)")

        self.cli_args = parser.parse_args()

        if self.cli_args.help:
            print("""
Fast PySlideshow - Image Viewer

Usage: python script.py [path] [options]

Options:
  path           Start scanning from specific folder
  --fullscreen   Start in fullscreen mode
  --shuffle      Start in Random mode (Default)
  --sequential   Start in Sequential mode (A-Z)
  --includeacr   Force enable archives
  --excludeacr   Force disable archives
  -?, --help     Show this help
            """)
            sys.exit(0)

        if self.cli_args.path: self.root_dir = os.path.abspath(self.cli_args.path)
        else: self.root_dir = self.root_dir = os.getcwd()

        if self.cli_args.includeacr: CFG_ARCHIVES_ENABLED = True
        elif self.cli_args.excludeacr: CFG_ARCHIVES_ENABLED = False

        if self.cli_args.sequential: global CFG_SLIDE_MODE; CFG_SLIDE_MODE = "sequential"
        if self.cli_args.duration: CFG_SLIDE_DURATION = self.cli_args.duration
        if self.cli_args.bg: CFG_BG_COLOR = self.cli_args.bg
        #if self.cli_args.force_duration: CFG_FORCE_MIN_DURATION = True

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
        
        # Кнопка переключения режима
        txt_mode = "RND" if self.slide_mode == 'sequential' else "SEQ"
        self.btn_mode = btn(txt_mode, self.toggle_slide_mode, 4, "Toggle Random/Sequential (Ctrl+S)")

        self.btn_lock = btn("HIDE", self.toggle_toolbar_lock, 5, "Lock Toolbar (Tab)")
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5, "Full Screen (F11)")
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 9, "Change Zoom (Z)")

        btn("CCR", lambda: self.rotate_image(-90), 4, "Rotate Left (Ctrl+E)")
        btn("CR", lambda: self.rotate_image(90), 4, "Rotate Right (Ctrl+R)")

        btn("<--", self.prev_image, 4, "Back (Left)")
        btn("-->", self.next_image, 4, "Next (Right)")

        btn("<<", self.first_file_folder, 3, "First in folder (Home)")
        btn("<-", self.prev_file_alpha, 3, "Prev File (Up)")
        btn("->", self.next_file_alpha, 3, "Next File (Down)")
        btn("^^", self.nav_folder_prev, 3, "Prev Folder (PgUp)")
        btn("vv", self.nav_folder_next, 3, "Next Folder (PgDn)")

        self.btn_play = btn("PAUSE", self.toggle_pause, 6, "Play/Pause (Space)")

        tk.Label(self.toolbar, text="Sec:", bg="#333333", fg="white").pack(side='left', padx=(5,0))
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION))
        self.speed_var.trace("w", self.on_speed_change)
        tk.Entry(self.toolbar, textvariable=self.speed_var, width=4).pack(side='left', padx=2)

        btn("FOLDER", self.open_current_folder, 7, "Open Folder (Enter)")

        self.lbl_info = tk.Label(self.toolbar, text="Init...", bg="#333333", fg=CFG_TEXT_COLOR, font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_preset())
        self.lbl_info.bind("<Button-3>", self.show_info_menu)

    def bind_events(self):
        # Навигация
        self.bind("<Right>", lambda e: self.next_image())
        self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<space>", lambda e: self.toggle_pause())
        
        # Внутри папки
        self.bind("<Up>", lambda e: self.prev_file_alpha())
        self.bind("<Down>", lambda e: self.next_file_alpha())
        self.bind("<Home>", lambda e: self.first_file_folder())
        
        # Папки
        self.bind("<Prior>", lambda e: self.nav_folder_prev()) # PgUp
        self.bind("<Next>", lambda e: self.nav_folder_next())  # PgDn
        
        # Функции
        self.bind("<Return>", lambda e: self.open_current_folder())
        self.bind("<F1>", lambda e: self.show_help())
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Alt-Return>", lambda e: self.toggle_fullscreen())
        self.bind("<F>", lambda e: self.toggle_fullscreen())

        # Zoom
        self.bind("z", lambda e: self.cycle_zoom())
        self.bind("Z", lambda e: self.cycle_zoom())
        self.bind("<Shift-KeyPress>", self.enable_temp_zoom)
        self.bind("<Shift-KeyRelease>", self.disable_temp_zoom)
        
        # Поворот (Ctrl/Cmd)
        is_mac = (platform.system() == 'Darwin')
        mod = "Command" if is_mac else "Control"
        
        self.bind(f"<{mod}-r>", lambda e: self.rotate_image(90))
        self.bind(f"<{mod}-R>", lambda e: self.rotate_image(90))
        self.bind(f"<{mod}-e>", lambda e: self.rotate_image(-90))
        self.bind(f"<{mod}-E>", lambda e: self.rotate_image(-90))
        
        # Переключение режима (Shuffle/Sequential)
        self.bind(f"<{mod}-s>", lambda e: self.toggle_slide_mode())
        self.bind(f"<{mod}-S>", lambda e: self.toggle_slide_mode())

        self.bind("i", lambda e: self.cycle_info_preset())
        self.bind("<Tab>", lambda e: self.toggle_toolbar_lock())
        
        # Мышь
        self.canvas.bind("<Motion>", self.check_toolbar_hover)
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<B1-Motion>", self.on_canvas_motion, add=True)
        self.canvas.bind("<Motion>", self.on_canvas_motion, add=True)

        self.canvas.bind("<Configure>", self.on_resize)

    # --- ЛОГИКА СКАНИРОВАНИЯ (Natural Sort) ---

    def start_initial_search(self):
        # Первый запуск: пытаемся быстро найти хоть что-то
        threading.Thread(target=self.find_first_image_task, daemon=True).start()

    def find_first_image_task(self):
        # Жадный поиск первого файла (использует дисковый метод)
        self.find_random_image_dynamic_disk(initial=True)
        time.sleep(0.5)
        # Запуск полного сканирования
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def update_loading_status(self, text):
        """Обновляет статус по центру экрана (только если картинки еще нет)."""
        def _u():
            # Если уже есть картинка, не рисуем поверх нее статус сканирования
            if self.image_shown_flag and "Scanning" in text: return
            
            # Удаляем старый текст статуса
            self.canvas.delete("status_text")
            
            # Если картинка уже есть, а мы хотим показать "Loading" следующей, 
            # можно не рисовать (чтобы не портить вид), либо рисовать мелко.
            # Но по вашей задаче "на черном экране" -> значит image_shown_flag False.
            
            if not self.image_shown_flag:
                cx, cy = self.winfo_width() // 2, self.winfo_height() // 2
                self.canvas.create_text(cx, cy, text=text, fill="#888888", 
                                      font=("Arial", 12), tags="status_text", width=800, justify='center')
        
        self.after(0, _u)

    def scan_worker(self):
        """
        Фоновое индексирование. Строит строго последовательный список (A-Z).
        Это позволяет работать Sequential режиму корректно, а Random будет брать
        случайные индексы из этого порядка.
        """
        temp = []
        last = time.time()

        def flush():
            nonlocal temp, last
            if temp:
                self.after(0, lambda b=list(temp): self.add_batch(b))
            temp = []
            last = time.time()

        # [FIX] Обработка случая, когда передан путь к файлу (архиву)
        if os.path.isfile(self.root_dir) and self.root_dir.lower().endswith('.zip') and CFG_ARCHIVES_ENABLED:
            try:
                with zipfile.ZipFile(self.root_dir, 'r') as zf:
                    names = sorted(zf.namelist(), key=lambda x: Utils.natural_keys(x))
                    for n in names:
                        if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                            temp.append(f"{VFS.PREFIX}{self.root_dir}{VFS.SEPARATOR}{n}")
            except: pass
            flush()
            self.is_scanning_active = False
            self.after(0, self.optimize_history_size)
            return

        for root, dirs, files in os.walk(self.root_dir):
            # Сортируем папки для правильного порядка обхода
            dirs.sort(key=lambda x: Utils.natural_keys(x))
            files.sort(key=lambda x: Utils.natural_keys(x))

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                fp = os.path.join(root, f)
                
                if ext in CFG_EXTENSIONS:
                    temp.append(fp)
                elif CFG_ARCHIVES_ENABLED and ext == '.zip':
                    try:
                        with zipfile.ZipFile(fp, 'r') as zf:
                            # Внутри архива тоже сортируем
                            names = sorted(zf.namelist(), key=lambda x: Utils.natural_keys(x))
                            for n in names:
                                if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                    temp.append(f"{VFS.PREFIX}{fp}{VFS.SEPARATOR}{n}")
                    except: pass

            if len(temp) > 1000 or (time.time() - last > 0.5 and temp):
                flush()
        
        flush()
        self.is_scanning_active = False
        
        # Проверка размера истории после сканирования
        # Если файлов меньше, чем длина истории, уменьшаем историю, чтобы избежать вечного цикла в Random
        self.after(0, self.optimize_history_size)

    def add_batch(self, b):
        self.all_files.extend(b)
        for p in b: self.folder_set.add(VFS.get_parent(p))
        if self.image_shown_flag and self.show_stats.get():
            self.update_info_label(None)

    def optimize_history_size(self):
        total = len(self.all_files)
        if total > 0 and total < self.history.maxlen:
            # Если файлов мало (например 100), а история 500, то Random будет долго искать уникальные.
            # Уменьшаем историю до 80% от кол-ва файлов, чтобы повторы случались чаще.
            new_len = max(1, int(total * 0.8))
            self.history = deque(self.history, maxlen=new_len)
            logging.info(f"History size optimized to {new_len} (Total files: {total})")

    # --- ЛОГИКА ВЫБОРА (SEQ / RND) ---

    def toggle_slide_mode(self):
        if self.slide_mode == 'random':
            self.slide_mode = 'sequential'
        else:
            self.slide_mode = 'random'
        
        txt = "SEQ" if self.slide_mode == 'sequential' else "RND"
        self.btn_mode.config(text=txt)
        self.title(f"Fast PySlideshow ({txt})")
        
        # Сброс таймера, чтобы следующее действие пошло по новой логике
        if not self.is_paused:
            self.reset_timer()

    def next_image(self):
        # 1. Навигация по истории вперед (если пользователь нажимал "Назад")
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            self.load_by_path(self.history[self.history_pointer])
            return

        # 2. Выбор нового файла
        next_path = None

        if self.slide_mode == 'sequential':
            # --- SEQUENTIAL MODE ---
            if not self.all_files: return
            
            try:
                idx = self.all_files.index(self.current_path)
            except ValueError:
                idx = -1
                if self.current_path:
                    try:
                        c_norm = os.path.normpath(self.current_path)
                        c_key = Utils.natural_keys(c_norm)
                        found = False
                        for i, f in enumerate(self.all_files):
                            if Utils.natural_keys(os.path.normpath(f)) > c_key:
                                idx = i - 1
                                found = True
                                break
                    except Exception as e:
                        logging.warning(f"Smart search error: {e}")
                        idx = -1
                
            next_idx = (idx + 1) % len(self.all_files)
            next_path = self.all_files[next_idx]

        else:
            # --- RANDOM MODE ---
            # Фаза 1: Сканирование активно -> Прыгаем по диску
            if self.is_scanning_active:
                threading.Thread(target=self.find_random_image_dynamic_disk, args=(False,), daemon=True).start()
                return 

            # Фаза 2: База набрана -> Выбор из RAM
            if self.all_files:
                # [MOD] Сначала проверяем, не выбрали ли мы файл заранее в prefetch
                if self.next_random_prepared:
                    next_path = self.next_random_prepared
                    self.next_random_prepared = None # Сброс после использования
                else:
                    # Если нет, выбираем сейчас
                    for _ in range(50): 
                        p = random.choice(self.all_files)
                        if p not in self.history:
                            next_path = p
                            break
                    if not next_path: next_path = random.choice(self.all_files)

        if next_path:
            self.load_by_path(next_path)
            self.history.append(next_path)
            self.history_pointer = len(self.history) - 1

    def find_random_image_dynamic_disk(self, initial=False):
        """
        Старый метод: прыгает по случайным папкам на диске.
        Используется ТОЛЬКО во время сканирования, чтобы не показывать одни 'A' файлы.
        """
        try:
            # [FIX] Если корень - это архив, сразу пытаемся взять оттуда
            if os.path.isfile(self.root_dir) and self.root_dir.lower().endswith('.zip') and CFG_ARCHIVES_ENABLED:
                self.try_pick_from_zip(self.root_dir, initial)
                return

            current = self.root_dir
            for i in range(50):
                if initial: self.update_loading_status(f"Scanning:\n{current}")
                # Проверка флага
                with self.image_shown_lock:
                    if initial and self.image_shown_flag: return

                try:
                    entries = list(os.scandir(current))
                except Exception:
                    if not initial: break 
                    current = self.root_dir; continue

                dirs = []
                files = []
                for e in entries:
                    if e.is_dir(): dirs.append(e.path)
                    elif e.is_file():
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in CFG_EXTENSIONS: files.append(e.path)
                        elif CFG_ARCHIVES_ENABLED and ext == '.zip': dirs.append(e.path)

                unseen = [f for f in files if f not in self.viewed_paths]
                
                # Шанс остановиться здесь
                pick_here = False
                if unseen:
                    if (not dirs) or (random.random() < 0.25): pick_here = True
                
                if pick_here:
                    t = random.choice(unseen)
                    self.after(0, lambda p=t: self.load_result_safe(p, initial))
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
            logging.error(f"Disk walker error: {e}")

    def load_result_safe(self, p, initial):
        with self.image_shown_lock:
            if initial and self.image_shown_flag: return
            if p in self.viewed_paths: return # Уже видели
            
            self.load_by_path(p)

            if not self.history or self.history[-1] != p:
                self.history.append(p)
	            #self.history_pointer = 0
                self.history_pointer = len(self.history) - 1
            
            if not self.is_paused:
                self.schedule_next_slide()

    def try_pick_from_zip(self, zp, initial):
        try:
            with zipfile.ZipFile(zp, 'r') as zf:
                names = [n for n in zf.namelist() if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS]
                if names:
                    p = f"{VFS.PREFIX}{zp}{VFS.SEPARATOR}{random.choice(names)}"
                    if p not in self.viewed_paths:
                        self.after(0, lambda: self.load_result_safe(p, initial))
                        return True
        except: pass
        return False

    def prev_image(self):
        if self.history_pointer > 0:
            self.history_pointer -= 1
            self.load_by_path(self.history[self.history_pointer])

    def load_by_path(self, path):
        # [FIX] 1. Сначала отменяем всё старое (меняем поколение)
        # Это гарантирует, что все последующие запросы (и display, и prefetch)
        # будут иметь новый, актуальный номер поколения.
        self.loader.cancel_all()
        
        self.current_path = path
        self.viewed_paths.add(path)
        self.rotation = 0
        self.last_valid_meta = None
        
        # 2. Запрашиваем текущее (оно получит новое поколение)
        self.display_current_image()
        self.reset_timer()
        
        # 3. Запускаем предзагрузку (она тоже будет использовать новое поколение)
        threading.Thread(target=self.schedule_prefetches, args=(path,), daemon=True).start()



    def schedule_prefetches(self, current_path):
        """
        Умная предзагрузка в строгом порядке:
        1. След. (Draft)
        2. След. (HQ)
        3. Тек. (Zoom 2x HQ)
        4. След. сосед (HQ)
        5. Тек. (Other zooms HQ)
        """
        # 1. Отменяем всё старое - не нужно
        #self.loader.cancel_all()
        
        # Определяем цели
        targets = []
        
        # A. Следующее выбранное (Random или Sequential)
        next_selected = None
        if self.slide_mode == 'sequential':
             # Логика Sequential (берем следующего соседа)
             sibs = VFS.list_siblings(current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
             if sibs:
                 try:
                     idx = sibs.index(current_path)
                     next_selected = sibs[(idx + 1) % len(sibs)]
                 except: pass
        else:
            # Логика Random (берем заранее подготовленного или выбираем)
            if self.next_random_prepared:
                next_selected = self.next_random_prepared
            else:
                # Если вдруг нет (первый запуск), выбираем сейчас
                if self.all_files:
                    for _ in range(50):
                         p = random.choice(self.all_files)
                         if p not in self.history and p != current_path:
                             next_selected = p; break
                    if not next_selected: next_selected = random.choice(self.all_files)
                    self.next_random_prepared = next_selected

        # B. Соседи (для ручной навигации) - следующий по алфавиту
        next_neighbor = None
        sibs = VFS.list_siblings(current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
        if sibs:
            try:
                i = sibs.index(current_path)
                next_neighbor = sibs[(i + 1) % len(sibs)]
            except: pass

        # --- ФОРМИРОВАНИЕ ОЧЕРЕДИ ---
        # Порядок подачи задач в executor_hq гарантирует порядок выполнения (FIFO)
        
        # 1. Следующее выбранное -> DRAFT
        if next_selected:
            self.loader.request_load(next_selected, self.zoom_mode, 0, is_draft=True)
            
        # 2. Следующее выбранное -> HQ
        if next_selected:
            self.loader.request_load(next_selected, self.zoom_mode, 0, is_draft=False)
            
        # 3. Текущее -> HQ Zoom 2x (Mode 3)
        self.loader.request_load(current_path, 3, 0, is_draft=False)
        
        # 4. Следующее по порядку (алфавиту) -> HQ (если отличается от selected)
        if next_neighbor and next_neighbor != next_selected:
            self.loader.request_load(next_neighbor, self.zoom_mode, 0, is_draft=False)
            
        # 5. Текущее -> Остальные зумы HQ
        # Текущий режим мы уже загрузили (мы его смотрим), так что грузим другие
        for m in [0, 1, 2]:
            if m != self.zoom_mode:
                self.loader.request_load(current_path, m, 0, is_draft=False)

    # --- НАВИГАЦИЯ ПО ПАПКАМ ---

    def nav_sibling(self, off):
        if not self.current_path: return
        sibs = VFS.list_siblings(self.current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
        if not sibs: return
        try:
            i = sibs.index(self.current_path)
            p = sibs[(i+off)%len(sibs)]
            
            self.load_by_path(p)
            # При ручной навигации добавляем в историю
            if not self.history or self.history[-1] != p:
                self.history.append(p)
                self.history_pointer = len(self.history)-1
        except:
            pass

    def next_file_alpha(self): self.nav_sibling(1)
    def prev_file_alpha(self): self.nav_sibling(-1)

    def first_file_folder(self):
        if self.current_path:
            s = VFS.list_siblings(self.current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
            if s:
                self.load_by_path(s[0])
                if not self.history or self.history[-1] != s[0]:
                    self.history.append(s[0])
                    self.history_pointer = len(self.history)-1

    def _folder_key(self, p):
        # Ключ для сортировки папок (уже не так нужен, т.к. список отсортирован, но пригодится для поиска)
        return Utils.natural_keys(p)

    def nav_folder_step(self, off):
        if not self.current_path: return
        cur = VFS.get_parent(self.current_path)
        
        # Преобразуем set в sorted list каждый раз (не очень эффективно, но надежно)
        # Лучше было бы поддерживать sorted list, но папки добавляются хаотично
        fs = sorted(list(self.folder_set), key=self._folder_key)
        
        if not fs: return
        try:
            i = fs.index(cur)
            target_folder = fs[(i+off)%len(fs)]
            
            if self.slide_mode == 'sequential':
                # В последовательном режиме открываем ПЕРВЫЙ файл папки
                self._load_first_in(target_folder)
            else:
                # В случайном режиме - случайный (старое поведение)
                self._load_rnd_in(target_folder)
        except:
            pass

    def nav_folder_next(self): self.nav_folder_step(1)
    def nav_folder_prev(self): self.nav_folder_step(-1)

    def _load_first_in(self, fld):
        files = self._get_files_in(fld)
        if files:
            # files уже отсортированы (natural)
            t = files[0]
            self.load_by_path(t)
            self.history.append(t)
            self.history_pointer = len(self.history)-1

    def _load_rnd_in(self, fld):
        files = self._get_files_in(fld)
        if files:
            t = random.choice(files)
            self.load_by_path(t)
            self.history.append(t)
            self.history_pointer = len(self.history)-1

    def _get_files_in(self, fld):
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
        
        # Сортируем всегда, чтобы 'first' был действительно первым
        files.sort(key=Utils.natural_keys)
        return files

    # --- ОТОБРАЖЕНИЕ ---

    def display_current_image(self):
        if not self.current_path: return
        
        self.current_image_is_final = False 
        
        w, h = self.winfo_width(), self.winfo_height()
        if self.toolbar_locked: h -= CFG_TOOLBAR_HEIGHT
        if h < 100: h = 100
        self.loader.update_screen_size(w, h)

        mode = 3 if self.temp_zoom else self.zoom_mode

        # 1. Пробуем взять из кэша
        cached = self.loader.get_image_direct(self.current_path, mode, self.rotation)
        
        if cached:
            # Есть в кэше -> показываем сразу
            self._update_canvas_safe(cached[0], cached[1])
            if not cached[1]:
                self.loader.request_load(self.current_path, mode, self.rotation, 
                                         is_draft=False, callback=self.on_image_ready_callback)
        else:
            # Нет в кэше.
            # [FIX] НЕ удаляем старую картинку, чтобы не было мигания "Loading..."
            # Вместо этого рисуем текст поверх текущего изображения (если нужно)
            
            # Удаляем только старые надписи "Loading", если они были
            if not self.image_shown_flag:
                msg = f"Loading Image:\n{self.current_path}"
                # Обрезаем слишком длинные строки для UI
                if len(msg) > 300: msg = "Loading Image:\n..." + msg[-300:]
                self.update_loading_status(msg)
            #self.canvas.delete("loading_text")
            
            # Можно добавить маленький индикатор в углу, что идет загрузка
            # cx, cy = self.winfo_width() // 2, self.winfo_height() // 2
            # self.canvas.create_text(cx, cy, text="Loading...", fill="white", tags="loading_text")
            
            # Запрашиваем загрузку
            self.loader.request_load(self.current_path, mode, self.rotation, 
                                     is_draft=True, callback=self.on_image_ready_callback)
            
            self.loader.request_load(self.current_path, mode, self.rotation, 
                                     is_draft=False, callback=self.on_image_ready_callback)

    def on_image_ready_callback(self, path, pil_img, is_final):
        """Вызывается из рабочего потока."""
        if path != self.current_path: return
        self.after(0, lambda: self._update_canvas_safe(pil_img, is_final))

    def _update_canvas_safe(self, pil, is_final):
        # Если мы уже показываем Final, не даем Draft'у его заменить
        if self.current_image_is_final and not is_final: return
        
        self.current_image_is_final = is_final
        tk_img = ImageTk.PhotoImage(pil)
        self._update_canvas(pil, tk_img)

    def _update_canvas(self, pil, tk_img):
        # [FIX] Теперь очищаем экран только здесь, когда новая картинка ГОТОВА
        self.canvas.delete("all") 
        
        if pil:
            self.last_valid_meta = pil.info.get('original_size', (pil.width, pil.height))
        else:
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, 
                                    text="Error", fill="white")
            if CFG_FORCE_MIN_DURATION: self.reset_timer()
            return

        if hasattr(self, 'current_tk_image'): del self.current_tk_image
        self.current_tk_image = tk_img

        cx = self.winfo_width() // 2
        cy = self.winfo_height() // 2
        if self.toolbar_locked:
            visible_h = self.winfo_height() - CFG_TOOLBAR_HEIGHT
            cy = visible_h // 2

        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags='img')
        
        if pil.width > self.winfo_width() or pil.height > self.winfo_height():
            self.update_zoom_pan()
        
        self.update_info_label(pil)

        if CFG_FORCE_MIN_DURATION:
            self.reset_timer()

    def on_image_ready_callback(self, path, pil_img, is_final):
        """Вызывается из рабочего потока."""
        # [FIX] Строгая проверка: если путь не совпадает с текущим, игнорируем
        if path != self.current_path: return
        self.after(0, lambda: self._update_canvas_safe(pil_img, is_final))

    def _update_canvas_safe(self, pil, is_final):
        """Обертка для обновления, чтобы не мигать, если уже Final"""
        if self.current_image_is_final and not is_final:
            return # Не заменяем хорошее на плохое
        
        self.current_image_is_final = is_final
        tk_img = ImageTk.PhotoImage(pil)
        
        # [MOD] Передаем is_final для отладки или логики (можно вывести (DRAFT) в info)
        self._update_canvas(pil, tk_img)

    # _update_canvas остается практически таким же, как в прошлом шаге
    # Но стоит добавить проверку is_final, чтобы не сбрасывать таймер дважды, если не хотим

    def _update_canvas(self, pil, tk_img):
        self.canvas.delete("all")
        if pil:
            self.last_valid_meta = pil.info.get('original_size', (pil.width, pil.height))
        else:
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, 
                                    text="Error/Loading...", fill="white")
            self.update_info_label(None)
            # Если ошибка, все равно запускаем таймер, чтобы пропустить файл
            if CFG_FORCE_MIN_DURATION: self.reset_timer() 
            return

        if hasattr(self, 'current_tk_image'): del self.current_tk_image
        self.current_tk_image = tk_img

        cx = self.winfo_width() // 2
        cy = self.winfo_height() // 2
        if self.toolbar_locked:
            visible_h = self.winfo_height() - CFG_TOOLBAR_HEIGHT
            cy = visible_h // 2

        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags='img')
        
        if pil.width > self.winfo_width() or pil.height > self.winfo_height():
            self.update_zoom_pan()
        
        self.update_info_label(pil)

        # Запускаем таймер, когда картинка реально показана
        if CFG_FORCE_MIN_DURATION:
            self.reset_timer()


    def update_info_label(self, img):
        if not self.current_path:
            self.lbl_info.config(text="")
            return
        
        p = [VFS.get_parent(self.current_path) + " \\"]
        if self.show_name.get(): p.append(VFS.get_name(self.current_path))
        
        if self.show_details.get():
            sz = Utils.format_size(VFS.get_size(self.current_path))
            w, h = None, None
            if img: w, h = img.info.get('original_size', (img.width, img.height))
            elif self.last_valid_meta: w, h = self.last_valid_meta
            res_str = f"[{w}x{h}]" if (w and h) else "[???]"
            p.append(f"{res_str} [{sz}]")

        if self.show_stats.get():
            # Показываем разную статистику в зависимости от режима
            if self.slide_mode == 'sequential':
                # Текущий индекс / Всего
                try:
                    curr = self.all_files.index(self.current_path) + 1
                except: curr = "?"
                total = len(self.all_files)
                p.append(f"({curr}/{total} in {len(self.folder_set)})")
            else:
                # Сколько просмотрено (Random style)
                v = len(self.viewed_paths)
                t = len(self.all_files)
                p.append(f"({v}/{t} in {len(self.folder_set)})")

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
            if self.toolbar_locked: h -= CFG_TOOLBAR_HEIGHT
            
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            cx, cy = w/2, h/2
            
            if iw <= w and ih <= h:
                self.canvas.coords('img', cx, cy)
                return

            if iw > w:
                ratio_x = max(0, min(1, event.x / w))
                cx = -(iw - w) * ratio_x + iw/2
            
            if ih > h:
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
        help_text = """
[Command Line]
path           : Start scanning from specific folder
--fullscreen   : Start in fullscreen mode
--shuffle      : Start in Random mode
--sequential   : Start in Sequential mode

[Navigation]
Right Arrow : Next Image
Left Arrow  : Previous Image (History)
Space       : Play / Pause
Enter       : Open File Location

[Folder Navigation]
PgDn        : Next Folder (First file)
PgUp        : Prev Folder (First file)
Down Arrow  : Next File in current folder
Up Arrow    : Prev File in current folder
Home        : First File in Folder

[View]
Z           : Cycle Zoom (Fit/Orig/Fill)
Shift (Hold): Temporary 2x Zoom (Loupe)
Ctrl+R      : Rotate Clockwise
Ctrl+E      : Rotate Counter-Clockwise
I           : Cycle Info Modes (Right click for menu)

[Window]
Tab            : Show / Hide Toolbar
F11 / Alt+Enter: Fullscreen
Esc            : Exit Fullscreen / Quit
F1             : This Help
Ctrl+S         : Toggle Random/Sequential
"""
        messagebox.showinfo("Help", help_text)

    def show_context_menu(self, e):
        m = tk.Menu(self, tearoff=0)
        
        # --- Навигация и Контроль ---
        # Меняем текст в зависимости от состояния
        state_label = "Resume Slideshow" if self.is_paused else "Pause Slideshow"
        m.add_command(label=state_label, command=self.toggle_pause)
        m.add_separator()
        
        m.add_command(label="Next Image", command=self.next_image)
        m.add_command(label="Previous Image", command=self.prev_image)
        
        # Подменю Режимов
        mode_menu = tk.Menu(m, tearoff=0)
        mode_menu.add_radiobutton(label="Random", 
                                  value='random', variable=tk.StringVar(value=self.slide_mode),
                                  command=lambda: self.set_slide_mode('random'))
        mode_menu.add_radiobutton(label="Sequential", 
                                  value='sequential', variable=tk.StringVar(value=self.slide_mode),
                                  command=lambda: self.set_slide_mode('sequential'))
        m.add_cascade(label="Slide Order", menu=mode_menu)
        
        m.add_separator()
        
        # --- Вид ---
        view_menu = tk.Menu(m, tearoff=0)
        view_menu.add_command(label="Toggle Fullscreen (F11)", command=self.toggle_fullscreen)
        view_menu.add_command(label="Cycle Zoom Mode (Z)", command=self.cycle_zoom)
        view_menu.add_command(label="Toggle Info Overlay (I)", command=self.cycle_info_preset)
        m.add_cascade(label="View", menu=view_menu)
        
        # --- Трансформация ---
        m.add_command(label="Rotate Right (Ctrl+R)", command=lambda: self.rotate_image(-90))
        m.add_command(label="Rotate Left (Ctrl+E)", command=lambda: self.rotate_image(90))

        m.add_separator()
        
        # --- Файл ---
        m.add_command(label="Open File Location", command=self.open_current_folder)
        m.add_command(label="Copy Path to Clipboard", command=self.copy_path_to_clipboard)
        
        m.add_separator()
        m.add_command(label="Exit", command=self.quit)
        
        m.tk_popup(e.x_root, e.y_root)

    # Вспомогательный метод для переключения режима через меню
    def set_slide_mode(self, mode):
        self.slide_mode = mode
        # Обновляем заголовок окна
        title_mode = "SEQ" if self.slide_mode == 'sequential' else "RND"
        self.title(f"Fast PySlideshow ({title_mode})")
        # Сбрасываем таймер, чтобы применить настройки
        self.reset_timer()

    # Вспомогательный метод для копирования пути
    def copy_path_to_clipboard(self):
        if self.current_path:
            self.clipboard_clear()
            self.clipboard_append(self.current_path)

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
        if py < ry or py > ry + self.winfo_height(): return
        y_pos = 0 if (self.winfo_height() - (py - ry) < CFG_TOOLBAR_TRIGGER_ZONE) else 100
        self.toolbar.place(relx=0, rely=1.0, y=y_pos, anchor='sw', relwidth=1.0)

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
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
        if not self.current_path: return
        self.is_paused = True
        self.btn_play.config(text="PLAY")
        
        if VFS.is_virtual(self.current_path):
            p = VFS.split_zip_path(self.current_path)[0]
        else:
            p = self.current_path
        
        p = os.path.normpath(os.path.abspath(p))
        if not os.path.exists(p): return

        sys_plat = platform.system()
        try:
            if sys_plat == 'Windows':
                subprocess.run(['explorer', '/select,', p])
            elif sys_plat == 'Darwin':
                subprocess.run(['open', '-R', p])
            elif sys_plat == 'Linux':
                try:
                    subprocess.run(['dbus-send', '--session', '--print-reply',
                                  '--dest=org.freedesktop.FileManager1',
                                  '/org/freedesktop/FileManager1',
                                  'org.freedesktop.FileManager1.ShowItems',
                                  f'array:string:file://{p}', 'string:'],
                                  check=True, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    subprocess.run(['xdg-open', os.path.dirname(p)])
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
