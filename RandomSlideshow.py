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
import re
from collections import deque, OrderedDict
from pathlib import Path
# rusauron, RandomSlideshow v0.594

# --- КОНФИГУРАЦИЯ (DEFAULTS) ---
CFG_ARCHIVES_ENABLED = True
CFG_SLIDE_DURATION = 4.0
CFG_SLIDE_MIN_DURATION = 0.02
CFG_FORCE_MIN_DURATION = False # Ждать полной загрузки перед запуском таймера (True) или включать таймер сразу (False)
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 10
CFG_TOOLBAR_HEIGHT = 40
CFG_SLIDE_MODE = "random" # random | sequential
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.tiff', '.webp', '.ico', '.avif'}
CFG_CACHE_SIZE = 20
CFG_MIN_FREE_RAM_MB = 512

# Настройка логирования
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ЗАВИСИМОСТИ ---

# 1. Pillow (Обязательно)
try:
    from PIL import Image, ImageTk, ImageOps, ImageFile, UnidentifiedImageError
    # Разрешаем загрузку обрезанных изображений (защита от крашей на битых файлах)
    ImageFile.LOAD_TRUNCATED_IMAGES = False 
except ImportError:
    logging.critical("CRITICAL: Pillow not found. Install it via 'pip install Pillow'")
    sys.exit(1)

# 2. HEIC Support (Опционально)
HEIC_SUPPORT = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
    CFG_EXTENSIONS.add('.heic'); CFG_EXTENSIONS.add('.heif')
except ImportError:
    pass

# 3. psutil для контроля RAM (Опционально)
PSUTIL_AVAILABLE = False
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    pass

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ---

class Utils:
    @staticmethod
    def format_size(size_bytes):
        """Форматирует байты в человекочитаемый вид (KB, MB)."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

    @staticmethod
    def natural_keys(text):
        """
        Ключ сортировки для естественного порядка (Natural Sort).
        Пример: file2.jpg идет перед file10.jpg.
        """
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

class VFS:
    """
    Виртуальная файловая система для прозрачной работы с ZIP архивами.
    Формат путей: zip:C:\path\to\archive.zip::internal/folder/image.jpg
    """
    SEPARATOR = "::"
    PREFIX = "zip:"

    @staticmethod
    def is_virtual(path):
        return path.startswith(VFS.PREFIX)

    @staticmethod
    def split_zip_path(path):
        """Разделяет виртуальный путь на путь к архиву и путь внутри архива."""
        if not VFS.is_virtual(path): return None, None
        content = path[len(VFS.PREFIX):]
        if VFS.SEPARATOR in content:
            return content.split(VFS.SEPARATOR, 1)
        return None, None

    @staticmethod
    def get_parent(path):
        """Возвращает родительскую директорию (реальную или виртуальную)."""
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            internal = internal.replace('\\', '/')
            if '/' not in internal: return archive # Родитель файла в корне архива - сам архив
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
        Безопасное чтение файла. Обернуто в try-except для защиты от битых архивов.
        """
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                # Защита: пробуем открыть архив
                try:
                    with zipfile.ZipFile(archive, 'r') as zf:
                        info = zf.getinfo(internal)
                        # Защита от ZIP-бомб (limit 1GB)
                        if info.file_size > 1024 * 1024 * 1024:
                            raise ValueError("File too large inside archive")
                        return zf.read(internal)
                except (zipfile.BadZipFile, RuntimeError) as e:
                    logging.error(f"ZIP Error {archive}: {e}")
                    raise IOError(f"Corrupted archive: {e}")
            else:
                safe_path = str(Path(path).resolve())
                # Windows long path fix
                if os.name == 'nt' and not safe_path.startswith('\\\\?\\'):
                    safe_path = '\\\\?\\' + safe_path
                with open(safe_path, 'rb') as f:
                    return f.read()
        except Exception as e:
            # Прокидываем ошибку выше, чтобы загрузчик узнал о проблеме
            raise e

    @staticmethod
    def list_siblings(path, extensions, sort_method=None):
        """Возвращает отсортированный список файлов в той же папке."""
        key_func = sort_method if sort_method else (lambda x: x.lower())
        
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                parent_internal = os.path.dirname(internal.replace('\\', '/'))
                siblings = []
                with zipfile.ZipFile(archive, 'r') as zf:
                    for name in zf.namelist():
                        name_norm = name.replace('\\', '/')
                        if os.path.dirname(name_norm) == parent_internal:
                            if os.path.splitext(name)[1].lower() in extensions:
                                siblings.append(f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{name}")
                siblings.sort(key=key_func)
                return siblings
            else:
                parent = os.path.dirname(path)
                files = [os.path.join(parent, f) for f in os.listdir(parent)
                         if os.path.splitext(f)[1].lower() in extensions]
                files.sort(key=key_func)
                return files
        except Exception as e:
            logging.warning(f"Error listing siblings for {path}: {e}")
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
    Кэш изображений с поддержкой черновиков (Draft) и защитой памяти.
    Хранит пары: key -> (PillowImage, is_final_hq)
    """
    def __init__(self, capacity, min_free_ram_mb):
        self.capacity = capacity
        self.min_free_ram_mb = min_free_ram_mb
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key):
        """Возвращает (image, is_final) или None."""
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def put(self, key, image, is_final):
        """Сохраняет изображение в кэш."""
        with self.lock:
            # Если мы пытаемся сохранить черновик, а там уже финал - не перезаписываем
            if key in self.cache:
                _, existing_is_final = self.cache[key]
                if existing_is_final and not is_final:
                    return

            self.cache[key] = (image, is_final)
            self.cache.move_to_end(key)
            self._cleanup()

    def _cleanup(self):
        """Очистка старых записей по лимиту количества или памяти."""
        # 1. Лимит по количеству
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        
        # 2. Лимит по RAM (если psutil доступен)
        if PSUTIL_AVAILABLE:
            try:
                # Если свободной памяти меньше лимита, агрессивно чистим
                while len(self.cache) > 1: # Оставляем хотя бы 1 картинку
                    mem = psutil.virtual_memory()
                    if mem.available / (1024*1024) < self.min_free_ram_mb:
                        logging.warning("Low RAM! Dropping cache item.")
                        self.cache.popitem(last=False)
                    else:
                        break
            except Exception:
                pass

class ImageLoader:
    """
    Менеджер загрузки. Обрабатывает очередь приоритетов.
    Использует ThreadPoolExecutor(max_workers=1) для строгой последовательности.
    """
    def __init__(self, app, cache_size, min_ram):
        self.app = app
        self.cache = ImageCache(cache_size, min_ram)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        
        # Токен отмены (поколение). Инкрементируется при смене цели.
        self.current_generation = 0
        self.gen_lock = threading.Lock()
        
        # Текущий путь цели (чтобы не перезапускать генерацию, если путь тот же)
        self.current_target_path = None
        self.target_lock = threading.Lock()

    def load_target(self, path, mode, rotation, screen_size, is_seq_mode, on_loaded_callback, on_error_callback):
        """
        Главный метод запуска цепочки задач (пункты 1-9 ТЗ).
        """
        with self.target_lock:
            # Пункт 1: Проверяем, меняется ли файл.
            is_new_file = (path != self.current_target_path)
            self.current_target_path = path
        
        # Если файл новый или поменялись параметры отображения, которые требуют перерисовки
        # Инкрементируем поколение. Это инвалидирует все старые задачи в очереди.
        # "Если имеющееся на экране... пусть продолжается" - это сложно реализовать в одном worker-e
        # без сложной логики. Проще всего: новая цель = новый приоритет. 
        # Старые задачи в очереди просто пропустятся worker-ом.
        with self.gen_lock:
            self.current_generation += 1
            gen_id = self.current_generation

        # Пункт 1: Выводим Loading сразу (если это смена файла)
        #if is_new_file:
        #     # Используем self вместо self.app
        #     self.update_info_text(f"Loading: {path}", is_loading=True)
        #     self.update() # Принудительно отрисовать текст ПЕРЕД началом загрузки тяжелой картинки

        if is_new_file:
             self.app.update_info_text(f"Loading: {VFS.get_name(path)}", is_loading=True)
             self.app.update() # Принудительно отрисовать текст ПЕРЕД началом загрузки тяжелой картинки

        # ФОРМИРОВАНИЕ ОЧЕРЕДИ ЗАДАЧ (Task Chaining)
        
        # 2. Текущее изображение -> Draft (Срочно)
        self.executor.submit(self._task_worker, path, mode, rotation, screen_size, True, gen_id, on_loaded_callback, on_error_callback)
        
        # 4. Текущее изображение -> HQ (Основное)
        self.executor.submit(self._task_worker, path, mode, rotation, screen_size, False, gen_id, on_loaded_callback, on_error_callback)
        
        # ОПРЕДЕЛЕНИЕ СОСЕДЕЙ ДЛЯ PREFETCH
        # Нам нужно вычислить пути соседей. Делаем это внутри executor (в отдельной задаче),
        # либо здесь. Здесь может быть долго (обращение к диску), лучше вынести расчет соседей в задачу.
        self.executor.submit(self._prefetch_planner_worker, path, mode, rotation, screen_size, is_seq_mode, gen_id)

    def _prefetch_planner_worker(self, current_path, mode, rotation, screen_size, is_seq_mode, gen_id):
        """
        Вычисляет, кого предзагружать (Пункты 3, 5, 6, 7, 8) и ставит задачи.
        """
        if not self._check_gen(gen_id): return

        # Получаем список соседей
        siblings = VFS.list_siblings(current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
        
        next_seq = None
        prev_seq = None
        next_rnd = None # Для режима Random

        if siblings:
            try:
                idx = siblings.index(current_path)
                next_seq = siblings[(idx + 1) % len(siblings)]
                prev_seq = siblings[(idx - 1) % len(siblings)]
            except ValueError:
                pass

        # Пункт 3: Если RND, нужно определить "случайное следующее".
        # Мы используем логику приложения. Но у нас нет доступа к app.history напрямую потокобезопасно.
        # Упрощение: В RND режиме мы предзагружаем то, что подготовило приложение (app.next_random_prepared)
        # Если его нет, то предзагрузка RND не сработает (не критично).
        if not is_seq_mode and self.app.next_random_prepared:
            next_rnd = self.app.next_random_prepared

        target_next = next_seq if is_seq_mode else next_rnd

        # --- ЗАПУСК PREFETCH ЗАДАЧ ---
        
        # 3. Следующее (SEQ или RND) -> Draft
        if target_next:
            self._submit_prefetch(target_next, mode, rotation, screen_size, True, gen_id)

        # 5. RND режим -> HQ Neighbor (Next Seq)
        # Полезно для ручной навигации стрелками в Random режиме
        if not is_seq_mode and next_seq and next_seq != target_next:
             self._submit_prefetch(next_seq, mode, rotation, screen_size, False, gen_id)

        # HQ для target_next (если SEQ, это дублирует пункт 5, кэш обработает)
        if target_next:
             self._submit_prefetch(target_next, mode, rotation, screen_size, False, gen_id)

        # 6. Предыдущее по алфавиту -> HQ
        if prev_seq:
            self._submit_prefetch(prev_seq, mode, rotation, screen_size, False, gen_id)
            
        # 7. Текущее -> 2x Zoom HQ (Mode 3)
        self._submit_prefetch(current_path, 3, rotation, screen_size, False, gen_id)
        
        # 8. Текущее -> Другие зумы
        for m in [0, 1, 2]:
            if m != mode:
                self._submit_prefetch(current_path, m, rotation, screen_size, False, gen_id)

    def _submit_prefetch(self, path, mode, rot, size, draft, gen):
        self.executor.submit(self._task_worker, path, mode, rot, size, draft, gen, None, None)

    def _check_gen(self, gen_id):
        with self.gen_lock:
            return gen_id == self.current_generation

    def _task_worker(self, path, mode, rotation, screen_size, is_draft, gen_id, on_loaded, on_error):
        """
        Рабочая лошадка. Декодирует, ресайзит, кэширует.
        """
        # 1. Быстрая проверка актуальности
        if not self._check_gen(gen_id): return

        key = (path, mode, rotation, screen_size)
        
        # 2. Проверка кэша (чтобы не делать двойную работу - Пункт 1)
        cached = self.cache.get(key)
        if cached:
            img, is_final = cached
            # Если у нас уже есть финал, а просят драфт - возвращаем финал (он лучше)
            if is_final:
                if on_loaded: on_loaded(path, img, True)
                return
            # Если просят драфт, и есть драфт - возвращаем
            if is_draft:
                if on_loaded: on_loaded(path, img, False)
                return
            # Если просят HQ, а есть только драфт -> идем дальше декодировать

        try:
            # Чтение байтов
            if not self._check_gen(gen_id): return
            data = VFS.read_bytes(path)
            
            if not self._check_gen(gen_id): return
            
            # --- ДЕКОДИРОВАНИЕ ---
            pil_img = Image.open(io.BytesIO(data))
            
            # Поворот (EXIF)
            pil_img = ImageOps.exif_transpose(pil_img)
            if rotation != 0:
                pil_img = pil_img.rotate(rotation, expand=True)

            iw, ih = pil_img.size
            sw, sh = screen_size
            
            # Расчет целевых размеров
            tw, th = iw, ih
            if mode == 0: # Fit
                ratio = min(sw/iw, sh/ih)
                tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 1: # Orig
                pass
            elif mode == 2: # Fill
                ratio = max(sw/iw, sh/ih)
                tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 3: # 2x
                tw, th = int(iw*2), int(ih*2)

            if tw < 1: tw = 1
            if th < 1: th = 1

            # --- DRAFT vs HQ ---
            resample_method = Image.Resampling.LANCZOS
            
            if is_draft:
                # Оптимизация для JPEG (используем встроенный draft декодер)
                if pil_img.format == 'JPEG':
                    try:
                        pil_img.draft('RGB', (tw, th))
                    except: pass
                resample_method = Image.Resampling.NEAREST # Быстро, но грубо

            # Ресайз
            if (tw, th) != (pil_img.size[0], pil_img.size[1]):
                pil_img = pil_img.resize((tw, th), resample_method)

            # Сохраняем оригинальный размер в info для отображения в UI
            pil_img.info['original_size'] = (iw, ih)

            # Сохраняем в кэш
            if not self._check_gen(gen_id): return
            self.cache.put(key, pil_img, not is_draft)

            # Callback в UI (только если это не prefetch)
            if on_loaded:
                on_loaded(path, pil_img, not is_draft)

        except Exception as e:
            logging.error(f"Error loading {path}: {e}")
            if on_error:
                on_error(path, str(e))


class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()
        
        # [FIX] Инициализация режима слайдшоу
        self.slide_mode = CFG_SLIDE_MODE or 'random'
        
        self.setup_window()
        
        # Данные
        self.all_files = [] # Sorted A-Z
        self.folder_set = set()
        self.viewed_paths = set()
        self.history = deque(maxlen=500)
        self.history_pointer = -1
        self.current_path = None
        self.next_random_prepared = None
        
        # Состояние
        self.is_paused = False
        self.slide_timer = None
        self.is_scanning_active = True
        self.zoom_mode = 0 # 0:Fit, 1:Orig, 2:Fill
        self.temp_zoom = False
        self.rotation = 0
        self.image_shown_flag = False  # Флаг для контроля отображения статуса "Scanning"
        self.current_tk_image = None   # Текущее отображаемое изображение (PhotoImage)
        self.toolbar_locked = True
        self.fullscreen = False
        self.was_locked_before_fs = True
        
        # UI Переменные
        self.show_path = tk.BooleanVar(value=True)
        self.show_name = tk.BooleanVar(value=True)
        self.show_details = tk.BooleanVar(value=True)
        self.show_stats = tk.BooleanVar(value=True)
        
        # Подсистемы
        self.loader = ImageLoader(self, self.cli_args.cache_size, self.cli_args.min_free_ram)
        
        self.setup_ui()
        self.bind_events()
        
        # Запуск сканирования
        self.start_initial_search()

    def parse_cli_args(self):
        global CFG_ARCHIVES_ENABLED, CFG_SLIDE_MODE, CFG_SLIDE_DURATION, CFG_BG_COLOR
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("path", nargs="?", default=None)
        parser.add_argument("--fullscreen", action="store_true")
        parser.add_argument("--shuffle", action="store_true")
        parser.add_argument("--sequential", "--seq", action="store_true")
        parser.add_argument("--includeacr", action="store_true")
        parser.add_argument("--excludeacr", action="store_true")
        parser.add_argument("--duration", "-t", type=float)
        parser.add_argument("--bg", type=str)
        parser.add_argument("--cache-size", type=int, default=CFG_CACHE_SIZE)
        parser.add_argument("--min-free-ram", type=int, default=CFG_MIN_FREE_RAM_MB)
        parser.add_argument("--help", "-h", "-?", action="store_true")
        
        self.cli_args = parser.parse_args()
        
        if self.cli_args.help:
            print("Usage: RandomSlideshow.py [path] [options]")
            sys.exit(0)
            
        if self.cli_args.path: self.root_dir = os.path.abspath(self.cli_args.path)
        else: self.root_dir = os.getcwd()
        
        if self.cli_args.includeacr: CFG_ARCHIVES_ENABLED = True
        elif self.cli_args.excludeacr: CFG_ARCHIVES_ENABLED = False
        
        if self.cli_args.sequential: CFG_SLIDE_MODE = "sequential"
        elif self.cli_args.shuffle: CFG_SLIDE_MODE = "random"
        
        if self.cli_args.duration: CFG_SLIDE_DURATION = self.cli_args.duration
        if self.cli_args.bg: CFG_BG_COLOR = self.cli_args.bg

    def setup_window(self):
        mode = "SEQ" if CFG_SLIDE_MODE == 'sequential' else "RND"
        self.title(f"RandomSlideshow ({mode})")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)
        if self.cli_args.fullscreen: self.toggle_fullscreen()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def cycle_info_preset(self):
        n, p, d = self.show_name.get(), self.show_path.get(), self.show_details.get()
        if n and p and d: self.show_path.set(0); self.show_details.set(0); self.show_stats.set(0)
        elif n and not p: self.show_path.set(1)
        elif p: self.show_name.set(0); self.show_path.set(0)
        else: self.show_name.set(1); self.show_path.set(1); self.show_details.set(1); self.show_stats.set(1)
        if self.current_path: self.update_info_text(self.current_path, getattr(self, 'current_pil', None))

    def setup_ui(self):
        # 1. Canvas (Основная область просмотра)
        self.canvas = tk.Canvas(self, bg=CFG_BG_COLOR, highlightthickness=0)
        self.canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)

        # 2. Toolbar Frame (Панель инструментов)
        self.toolbar = tk.Frame(self, bg="#333333", height=CFG_TOOLBAR_HEIGHT)
        self.toolbar.pack_propagate(False)
        self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

        # Вспомогательная функция для создания кнопок с подсказками
        def btn(text, command, width=None, tt=""):
            b = tk.Button(self.toolbar, text=text, command=command, width=width)
            b.pack(side='left', padx=2)
            if tt: 
                ToolTip(b, tt) # Подключаем класс ToolTip
            return b

        # --- Наполнение панели инструментов ---

        # Help
        btn("?", self.show_help, 2, "Help (F1)")

        # Mode (SEQ / RND)
        # Определяем начальный текст кнопки в зависимости от глобальной настройки
        txt_mode = "SEQ" if CFG_SLIDE_MODE == 'sequential' else "RND"
        self.btn_mode = btn(txt_mode, self.toggle_slide_mode, 4, "Toggle Random/Sequential (Ctrl+S)")

        # Toolbar Lock (HIDE / SHOW)
        self.btn_lock = btn("HIDE", self.toggle_toolbar_lock, 6, "Lock/Unlock Toolbar (Tab)")

        # Fullscreen
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5, "Toggle Fullscreen (F11/Alt+Enter)")

        # Zoom
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 9, "Change Zoom Mode (Z)")

        # Rotation (CCR / CR)
        btn("CCR", lambda: self.rotate_image(-90), 4, "Rotate Left (Ctrl+E)")
        btn("CR", lambda: self.rotate_image(90), 4, "Rotate Right (Ctrl+R)")

        # Main Navigation (<-- / -->)
        btn("<--", self.prev_image, 4, "Previous Image (Left)")
        btn("-->", self.next_image, 4, "Next Image (Right)")

        # Folder Navigation (восстановленные кнопки)
        btn("<<", self.first_file_folder, 3, "First in Folder (Home)")
        btn("<-", self.prev_file_alpha, 3, "Prev File in Folder (Up)")
        btn("->", self.next_file_alpha, 3, "Next File in Folder (Down)")
        btn("^^", self.nav_folder_prev, 3, "Prev Folder (PgUp)")
        btn("vv", self.nav_folder_next, 3, "Next Folder (PgDn)")

        # Play / Pause
        self.btn_play = btn("PAUSE", self.toggle_pause, 6, "Play/Pause Slideshow (Space/P)")

        # Speed Control (Sec:)
        tk.Label(self.toolbar, text="Sec:", bg="#333333", fg="white").pack(side='left', padx=(5,0))
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION))
        # Привязываем изменение значения, если метод существует
        if hasattr(self, 'on_speed_change'):
            self.speed_var.trace("w", self.on_speed_change)
        tk.Entry(self.toolbar, textvariable=self.speed_var, width=4).pack(side='left', padx=2)

        # Folder Open
        btn("FOLDER", self.open_current_folder, 7, "Open Current Folder (Enter)")

        # Info Label (Справа)
        self.lbl_info = tk.Label(self.toolbar, text="Init...", bg="#333333", fg=CFG_TEXT_COLOR, font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        
        # Восстанавливаем кликабельность инфо-панели
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_preset())
        self.lbl_info.bind("<Button-3>", self.show_info_menu)




    def bind_events(self):
        # Навигация
        self.bind("<Right>", lambda e: self.next_image())
        self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<space>", lambda e: self.toggle_pause())
        self.bind("<p>", lambda e: self.toggle_pause())
        self.bind("<P>", lambda e: self.toggle_pause())
        
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
        self.bind("<f>", lambda e: self.toggle_fullscreen())

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
        # Zoom Loupe (Shift)
        self.bind("<Shift_L>", self.enable_temp_zoom)
        self.bind("<KeyRelease-Shift_L>", self.disable_temp_zoom)
        
        # Mouse
        self.canvas.bind("<Motion>", self.check_toolbar_hover)
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Motion>", self.on_canvas_motion, add=True)
        
        # Resize
        self.bind("<Configure>", self.on_resize)

        # Initial Layout Update
        self.update_layout()

    # --- ЛОГИКА СКАНИРОВАНИЯ ---
    def start_initial_search(self):
        # [FIX] Если указан конкретный файл (архив), сканер диска (scandir) сломается.
        # Мы полагаемся только на scan_worker, который быстро распарсит архив.
        if not os.path.isfile(self.root_dir):
            # Поток для прыжков по диску (быстрый старт)
            threading.Thread(target=self.find_first_image_task, daemon=True).start()
        
        # Поток полного индексирования
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def find_first_image_task(self):
        # "Жадный" поиск на диске пока сканирование идет
        self.find_random_image_dynamic_disk(initial=True)

    def scan_worker(self):
        # Полное сканирование в all_files (сортировка A-Z)
        temp = []
        last_flush = time.time()
        
        def flush():
            nonlocal temp, last_flush
            if temp:
                self.after(0, lambda b=list(temp): self.add_batch(b))
                temp = []
            last_flush = time.time()

        # Если корень - архив
        if os.path.isfile(self.root_dir) and self.root_dir.lower().endswith('.zip') and CFG_ARCHIVES_ENABLED:
            try:
                with zipfile.ZipFile(self.root_dir, 'r') as zf:
                    names = sorted(zf.namelist(), key=Utils.natural_keys)
                    for n in names:
                        if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                            temp.append(f"{VFS.PREFIX}{self.root_dir}{VFS.SEPARATOR}{n}")
            except: pass
        else:
            # Обычный обход
            for root, dirs, files in os.walk(self.root_dir):
                dirs.sort(key=Utils.natural_keys)
                files.sort(key=Utils.natural_keys)
                
                for f in files:
                    if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS:
                        temp.append(os.path.join(root, f))
                
                if CFG_ARCHIVES_ENABLED:
                    for f in files:
                        if f.lower().endswith('.zip'):
                            fp = os.path.join(root, f)
                            try:
                                with zipfile.ZipFile(fp, 'r') as zf:
                                    names = sorted(zf.namelist(), key=Utils.natural_keys)
                                    for n in names:
                                        if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                            temp.append(f"{VFS.PREFIX}{fp}{VFS.SEPARATOR}{n}")
                            except: pass
                
                if len(temp) > 500 or (time.time() - last_flush > 0.5):
                    flush()
        
        flush()
        self.is_scanning_active = False

    def add_batch(self, batch):
        self.all_files.extend(batch)
        for p in batch: self.folder_set.add(VFS.get_parent(p))

        # [FIX] Если ничего не воспроизводится и пришли первые файлы (актуально для ZIP)
        if not self.current_path and self.all_files:
            # Выбираем стартовый файл
            if CFG_SLIDE_MODE == 'sequential':
                start_path = self.all_files[0]
            else:
                start_path = random.choice(self.all_files)
            
            # Инициируем загрузку
            self._initial_load_callback(start_path, True)


    # --- НАВИГАЦИЯ И ЗАГРУЗКА ---

    def next_image(self):
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            self.load_by_path(self.history[self.history_pointer])
            return

        next_path = None
        
        if CFG_SLIDE_MODE == 'sequential':
            # SEQ Logic
            idx = -1
            if self.all_files:
                try: idx = self.all_files.index(self.current_path)
                except: pass
                next_path = self.all_files[(idx + 1) % len(self.all_files)]
        else:
            # RND Logic
            if self.is_scanning_active:
                threading.Thread(target=self.find_random_image_dynamic_disk, args=(False,), daemon=True).start()
                return # Disk walker загрузит сам
            
            if self.next_random_prepared:
                next_path = self.next_random_prepared
                self.next_random_prepared = None
            elif self.all_files:
                next_path = random.choice(self.all_files)
        
        if next_path:
            self.load_by_path(next_path)
            self.history.append(next_path)
            self.history_pointer = len(self.history) - 1

    def prev_image(self):
        if self.history_pointer > 0:
            self.history_pointer -= 1
            self.load_by_path(self.history[self.history_pointer])

    def find_random_image_dynamic_disk(self, initial=False):
        curr = self.root_dir
    
        # Обновляем инфо при поиске
        if initial: 
            self.update_info_text(f"Scanning: {curr}", is_loading=True)
            self.update() # Важно: обновить UI, иначе текст не появится до конца сканирования
    
        for _ in range(100):
            try:
                entries = list(os.scandir(curr))
                dirs = [e.path for e in entries if e.is_dir()]
                files = [
                    e.path for e in entries
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in CFG_EXTENSIONS
                ]
    
                # Если нашли файлы — обрабатываем с вероятностью 30% (как раньше)
                if files and (not dirs or random.random() < 0.3):
                    p = random.choice(files)
                    self.after(0, lambda: self._initial_load_callback(p, initial))
                    return
    
                # Если есть поддиректории — идём в случайную
                if dirs:
                    curr = random.choice(dirs)
                else:
                    # Нет поддиректорий И нет файлов — применяем новую логику
                    if not files and curr != self.root_dir:  # Не в корне и пусто
                        if random.random() < 0.5:
                            # 50% — подняться на уровень выше
                            curr = os.path.dirname(curr)
                        else:
                            # 50% — вернуться в корневую директорию
                            curr = self.root_dir
                    else:
                        # Если в корне или есть файлы (но нет директорий) — прерываем
                        break
    
                if initial:
                    self.update_info_text(f"Scanning: {curr}", is_loading=True)
    
            except Exception as e:
                # Лучше логгировать ошибку (опционально)
                # print(f!Error scanning {curr}: {e}")
                break



    def _initial_load_callback(self, p, initial):
        if initial and self.current_path: return
        self.load_by_path(p)
        
        # [FIX] Всегда добавляем в историю, чтобы работала кнопка Prev
        # Проверяем на дубликат последнего элемента, чтобы не двоить
#        if not self.history or self.history[-1] != p:
        self.history.append(p)
        self.history_pointer = len(self.history) - 1
        
        if not self.is_paused: self.reset_timer()


    def load_by_path(self, path):
        self.current_path = path
        self.viewed_paths.add(path)
        self.rotation = 0
        
        # Таймер: Если Force Min Duration включен, мы его ставим ПОСЛЕ загрузки
        # Если выключен - ставим СЕЙЧАС (чтобы время загрузки шло в зачет)
        if not CFG_FORCE_MIN_DURATION and not self.is_paused:
            self.reset_timer()
        elif CFG_FORCE_MIN_DURATION:
            if self.slide_timer: self.after_cancel(self.slide_timer)
            self.slide_timer = None

        # Запускаем загрузчик
        w, h = self.get_canvas_size()
        mode = 3 if self.temp_zoom else self.zoom_mode
        is_seq = (CFG_SLIDE_MODE == 'sequential')
        
        # Подготовка случайного следующего для RND
        if not is_seq and self.all_files:
            self.next_random_prepared = random.choice(self.all_files)

        self.loader.load_target(path, mode, 0, (w, h), is_seq, 
                                self.on_image_loaded, self.on_image_error)

    # --- CALLBACKS ЗАГРУЗЧИКА ---

    def on_image_loaded(self, path, pil_image, is_final):
        """Callback после загрузки изображения."""
        if path != self.current_path:
            return  # Игнорируем устаревшие результаты
        
        # [FIX] Защита от замены Final на Draft
        if hasattr(self, '_showing_final_for_path') and \
           self._showing_final_for_path == path and not is_final:
            return
    
        try:
            new_tk_image = ImageTk.PhotoImage(pil_image)
            
            cw, ch = self.get_canvas_size()
            iw, ih = pil_image.size # Новые размеры
            
            # --- УМНОЕ ПОЗИЦИОНИРОВАНИЕ (Zoom to Mouse) ---
            
            # По умолчанию: центрируем
            target_x = (cw - iw) // 2
            target_y = (ch - ih) // 2
            
            should_update_pan = True

            # Проверяем, есть ли старое изображение и тот ли это файл
            # (Если файл новый, то центрирование по умолчанию корректно)
            # Но если это зум (тот же путь), пытаемся сохранить фокус
            if path == getattr(self, '_current_image_path_on_screen', None):
                
                # Ищем текущую картинку
                cur_items = self.canvas.find_withtag("current_image")
                if cur_items:
                    # Координаты старого изображения (Top-Left)
                    old_x, old_y = self.canvas.coords(cur_items[0])
                    
                    # Размеры старого изображения (берем из stored reference)
                    if hasattr(self, 'current_tk_image') and self.current_tk_image:
                        old_w = self.current_tk_image.width()
                        old_h = self.current_tk_image.height()
                        
                        # Координаты курсора мыши
                        mx = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
                        my = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
                        
                        # Вычисляем относительную позицию курсора на СТАРОЙ картинке (0.0 ... 1.0)
                        # (mx - old_x) — это смещение курсора от левого края картинки
                        if old_w > 0 and old_h > 0:
                            rel_x = (mx - old_x) / old_w
                            rel_y = (my - old_y) / old_h
                            
                            # Теперь вычисляем новый Top-Left (target_x, target_y) так,
                            # чтобы точка (rel_x, rel_y) на НОВОЙ картинке совпала с mx, my
                            # mx = new_x + new_w * rel_x  =>  new_x = mx - new_w * rel_x
                            
                            target_x = int(mx - (iw * rel_x))
                            target_y = int(my - (ih * rel_y))
                            
                            should_update_pan = False # Мы сами рассчитали позицию, авто-пан не нужен

            # Запоминаем текущий путь
            self._current_image_path_on_screen = path

            # 1. Рисуем
            new_img_id = self.canvas.create_image(target_x, target_y, anchor='nw', image=new_tk_image, tags="new_image")
            
            # 2. Удаляем старое
            self.canvas.delete("current_image")
            
            # 3. Удаляем статус
            #self.canvas.delete("status_text")
            
            # 4. Теги
            self.canvas.dtag(new_img_id, "new_image")
            self.canvas.addtag_withtag("current_image", new_img_id)
            self.canvas.tag_lower("current_image")
            
            self.current_tk_image = new_tk_image
            self.current_pil = pil_image # Для инфо
            
            if is_final:
                self._showing_final_for_path = path
            else:
                if getattr(self, '_showing_final_for_path', None) != path:
                    self._showing_final_for_path = None
    
            self.update_info_text(path, pil_image, is_loading=False)
            
            if is_final and CFG_FORCE_MIN_DURATION and not self.is_paused:
                self.reset_timer()
                
            # Если картинка большая и мы не рассчитывали зум вручную (например, это новый файл)
            # вызываем стандартный апдейт
            if should_update_pan and (iw > cw or ih > ch):
                 self.update_zoom_pan()
            
        except Exception as e:
            logging.error(f"Display error: {e}")


    
    def on_image_error(self, path, error_msg):
        """Callback при ошибке загрузки."""
        logging.warning(f"Failed to load {path}: {error_msg}")
        # Пробуем следующее изображение
        self.after(100, self.next_image)

    def on_image_error(self, path, err_msg):
        self.after(0, lambda: self._handle_error(path, err_msg))

    def _handle_error(self, path, err_msg):
        logging.error(f"Failed: {err_msg}")
        self.update_info_text(f"Error: {path} ({err_msg})", is_loading=True)
        
        # [FIX] Переносим битый файл в начало истории, чтобы он не мешал навигации "Назад"
        if self.history and path in self.history:
            try:
                self.history.remove(path)
                self.history.insert(0, path)
                # Корректируем указатель: теперь он должен указывать на последний валидный элемент
                # (т.к. мы удалили текущий битый с конца, указатель сместился или стал указывать на последний валидный)
                self.history_pointer = len(self.history) - 1
            except ValueError:
                pass

        # Авто-пропуск битых файлов (даже если пауза, лучше уйти с битого)
        # Убрал проверку is_paused, чтобы не зависать на ошибке, или можно оставить по желанию
        self.after(500, self.next_image)


    # --- UI UPDATES ---

    def update_info_text(self, text_or_path, img=None, is_loading=False):
        """
        Пункт 10/15: Форматирование строки состояния.
        Thread-safe: использует after, если вызван не из главного потока.
        """
        # Проверка: если вызваны из другого потока, перенаправляем в main loop
        if threading.current_thread() is not threading.main_thread():
             self.after(0, lambda: self.update_info_text(text_or_path, img, is_loading))
             return

        # Проверка на существование виджета (защита при закрытии)
        if not hasattr(self, 'lbl_info') or not self.lbl_info.winfo_exists():
            return

        if is_loading:
            self.lbl_info.config(text=text_or_path)
            return

        parts = []
        path = text_or_path
        
        # Path / Name
        parent = VFS.get_parent(path) + ("\\" if os.name=='nt' else "/")
        name = VFS.get_name(path)
        
        if self.show_path.get() and self.show_name.get(): parts.append(f"{parent}{name}")
        elif self.show_name.get(): parts.append(name)
        elif self.show_path.get(): parts.append(parent)
        
        # Details
        if self.show_details.get():
            orig_w, orig_h = "?", "?"
            if img: orig_w, orig_h = img.info.get('original_size', (img.width, img.height))
            size_str = Utils.format_size(VFS.get_size(path))
            parts.append(f"| {orig_w}x{orig_h} | {size_str}")
        
        # Stats
        if self.show_stats.get():
            #hist_len = len(self.history)
            if self.slide_mode == 'sequential':
                hist_len = self.all_files.index(self.current_path) + 1
            else:
                hist_len = len(self.viewed_paths)

            scanned = len(self.all_files)
            folders = len(self.folder_set)
            parts.append(f"| {hist_len}/{scanned} in {folders}")

        final_text = " ".join(parts)
        self.lbl_info.config(text=final_text)

    def update_layout(self):
        """Пункт 12: Панель уменьшает доступную область."""
        w = self.winfo_width()
        h = self.winfo_height()
        
        if self.toolbar_locked:
            # Тулбар занимает место
            self.canvas.place(x=0, y=0, width=w, height=h - CFG_TOOLBAR_HEIGHT)
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        else:
            # Тулбар плавающий (скрыт или поверх)
            self.canvas.place(x=0, y=0, width=w, height=h)
            # Логика hover определит позицию тулбара

    def get_canvas_size(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if self.toolbar_locked: h -= CFG_TOOLBAR_HEIGHT
        return max(1, w), max(1, h)

    def on_resize(self, event):
        if event.widget == self:
            self.update_layout()
            # Перезапуск отображения с задержкой (debounce)
            if hasattr(self, '_resize_job'): self.after_cancel(self._resize_job)
            self._resize_job = self.after(200, lambda: self.load_by_path(self.current_path) if self.current_path else None)

    def on_canvas_motion(self, event):
        """Панорамирование изображения, если оно больше экрана."""
        # Если картинки нет, выходим
        if not hasattr(self, 'current_tk_image') or not self.current_tk_image:
            return

        # Получаем размеры области просмотра и изображения
        w, h = self.get_canvas_size()
        iw, ih = self.current_tk_image.width(), self.current_tk_image.height()

        # Базовая позиция - по центру
        target_x = (w - iw) // 2
        target_y = (h - ih) // 2

        # Если изображение шире экрана - вычисляем смещение по X
        if iw > w:
            # event.x может выходить за пределы (если мышь ушла быстро), ограничиваем 0..w
            mouse_x = max(0, min(w, event.x))
            ratio_x = mouse_x / w
            # Смещаем так, чтобы при ratio=0 был левый край (0), при ratio=1 - правый (w-iw)
            target_x = int(-(iw - w) * ratio_x)

        # Если изображение выше экрана - вычисляем смещение по Y
        if ih > h:
            mouse_y = max(0, min(h, event.y))
            ratio_y = mouse_y / h
            target_y = int(-(ih - h) * ratio_y)

        # Применяем координаты. Используем тег "current_image", добавленный в фиксе v54
        # Если тега нет (старая версия), пробуем найти все картинки
        self.canvas.coords("current_image", target_x, target_y)

    def update_zoom_pan(self):
        """Принудительно обновляет позицию (например, после загрузки)."""
        # Эмулируем событие мыши в текущей позиции курсора
        x = self.winfo_pointerx() - self.canvas.winfo_rootx()
        y = self.winfo_pointery() - self.canvas.winfo_rooty()
        
        class MockEvent: pass
        e = MockEvent()
        e.x, e.y = x, y
        self.on_canvas_motion(e)


    # --- УПРАВЛЕНИЕ ---

    def toggle_slide_mode(self):
        global CFG_SLIDE_MODE
        CFG_SLIDE_MODE = "sequential" if CFG_SLIDE_MODE == "random" else "random"
        txt = "SEQ" if CFG_SLIDE_MODE == 'sequential' else "RND"
        self.btn_mode.config(text=txt)
        self.title(f"RandomSlideshow v50 ({txt})")

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        self.update_layout()
        if self.current_path: self.load_by_path(self.current_path)

    def toggle_fullscreen(self, force_exit=False):
        if force_exit: self.fullscreen = False
        else: self.fullscreen = not self.fullscreen
        
        self.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen:
            self.was_locked_before_fs = self.toolbar_locked
            self.toolbar_locked = False
        else:
            self.toolbar_locked = self.was_locked_before_fs
        
        self.toggle_toolbar_lock() # Обновит UI и текст кнопки
        # Инвертируем обратно, т.к. toggle переключил
        self.toolbar_locked = not self.toolbar_locked 
        self.update_layout()

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.btn_play.config(text="PLAY" if self.is_paused else "PAUSE")
        if not self.is_paused: self.reset_timer()
        elif self.slide_timer: 
            self.after_cancel(self.slide_timer)
            self.slide_timer = None

    def cycle_zoom(self):
        self.zoom_mode = (self.zoom_mode + 1) % 3
        modes = ["ZOOM Fit", "ZOOM Orig", "ZOOM Fill"]
        self.btn_zoom.config(text=modes[self.zoom_mode])
        if self.current_path: self.load_by_path(self.current_path)

    # [FIX] Переименовали, чтобы совпадало с вызовом в кнопках (rotate_image)
    def rotate_image(self, deg):
        self.rotation = (self.rotation - deg) % 360
        if self.current_path: 
            w, h = self.get_canvas_size()
            mode = 3 if self.temp_zoom else self.zoom_mode
            is_seq = (CFG_SLIDE_MODE == 'sequential')
            self.loader.load_target(self.current_path, mode, self.rotation, (w, h), is_seq, 
                                    self.on_image_loaded, self.on_image_error)
        if not self.is_paused: self.reset_timer()

    # --- ВОССТАНОВЛЕННЫЕ МЕТОДЫ НАВИГАЦИИ ---

    def nav_sibling(self, off):
        if not self.current_path: return
        sibs = VFS.list_siblings(self.current_path, CFG_EXTENSIONS, sort_method=Utils.natural_keys)
        if not sibs: return
        try:
            i = sibs.index(self.current_path)
            p = sibs[(i+off)%len(sibs)]
            self.load_by_path(p)
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
        return Utils.natural_keys(p)

    def nav_folder_step(self, off):
        if not self.current_path: return
        cur = VFS.get_parent(self.current_path)
        fs = sorted(list(self.folder_set), key=self._folder_key)
        if not fs: return
        try:
            i = fs.index(cur)
            target_folder = fs[(i+off)%len(fs)]
            if self.slide_mode == 'sequential':
                self._load_first_in(target_folder)
            else:
                self._load_rnd_in(target_folder)
        except:
            pass

    def nav_folder_next(self): self.nav_folder_step(1)
    def nav_folder_prev(self): self.nav_folder_step(-1)

    def _load_first_in(self, fld):
        files = self._get_files_in(fld)
        if files:
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
        files.sort(key=Utils.natural_keys)
        return files

    def enable_temp_zoom(self, e):
        if not self.temp_zoom:
            self.temp_zoom = True
            if self.current_path: self.load_by_path(self.current_path)

    def disable_temp_zoom(self, e):
        if self.temp_zoom:
            self.temp_zoom = False
            if self.current_path: self.load_by_path(self.current_path)

    def check_toolbar_hover(self, e):
        if self.toolbar_locked: return
        h = self.winfo_height()
        if e.y > h - CFG_TOOLBAR_TRIGGER_ZONE:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        else:
            self.toolbar.place(relx=0, rely=1.0, y=CFG_TOOLBAR_HEIGHT, anchor='sw', relwidth=1.0)

    def on_speed_change(self, *args):
        try:
            val = float(self.speed_var.get().replace(',', '.'))
            global CFG_SLIDE_DURATION
            CFG_SLIDE_DURATION = max(val, CFG_SLIDE_MIN_DURATION)
        except: pass

    def reset_timer(self):
        if self.slide_timer: self.after_cancel(self.slide_timer)
        self.slide_timer = self.after(int(CFG_SLIDE_DURATION * 1000), self.next_image)

    def on_close(self):
        logging.info("Shutting down...")
        self.destroy()
        sys.exit(0)

    # --- MENUS ---
    def show_context_menu(self, e):
        m = tk.Menu(self, tearoff=0)
        
        # --- Навигация и Контроль ---
        state_label = "Resume Slideshow (Space)" if self.is_paused else "Pause Slideshow (Space)"
        m.add_command(label=state_label, command=self.toggle_pause)
        m.add_separator()
        
        m.add_command(label="Next Image (Right)", command=self.next_image)
        m.add_command(label="Previous Image (Left)", command=self.prev_image)
        
        # Подменю Режимов
        mode_menu = tk.Menu(m, tearoff=0)
        # Используем текущий режим для отображения галочки (эмуляция радиокнопки через check)
        current_mode = CFG_SLIDE_MODE # Используем глобальную переменную v52
        mode_menu.add_command(label=f"Random {'(Active)' if current_mode=='random' else ''}", 
                              command=lambda: self.set_slide_mode_direct('random'))
        mode_menu.add_command(label=f"Sequential {'(Active)' if current_mode=='sequential' else ''}", 
                              command=lambda: self.set_slide_mode_direct('sequential'))
        m.add_cascade(label="Slide Order (Ctrl+S)", menu=mode_menu)
        
        m.add_separator()
        
        # --- Вид ---
        view_menu = tk.Menu(m, tearoff=0)
        view_menu.add_command(label="Toggle Fullscreen (F11)", command=self.toggle_fullscreen)
        view_menu.add_command(label="Cycle Zoom Mode (Z)", command=self.cycle_zoom)
        view_menu.add_command(label="Toggle Info Overlay (I)", command=self.cycle_info_preset)
        m.add_cascade(label="View", menu=view_menu)
        
        # --- Трансформация ---
        m.add_command(label="Rotate Left (Ctrl+E)", command=lambda: self.rotate_image(-90))
        m.add_command(label="Rotate Right (Ctrl+R)", command=lambda: self.rotate_image(90))
    
        m.add_separator()
        
        # --- Файл ---
        m.add_command(label="Open File Location (Enter)", command=self.open_current_folder)
        m.add_command(label="Copy Path to Clipboard", command=self.copy_path)
        
        m.add_separator()
        m.add_command(label="Exit", command=self.on_close)
        
        m.tk_popup(e.x_root, e.y_root)
    
    # Вспомогательный метод для меню (добавить в класс)
    def set_slide_mode_direct(self, mode):
        global CFG_SLIDE_MODE
        if CFG_SLIDE_MODE != mode:
            self.toggle_slide_mode()

    def show_info_menu(self, e):
        m = tk.Menu(self, tearoff=0)
        m.add_checkbutton(label="Show Path", variable=self.show_path, command=lambda: self.update_info_text(self.current_path, self.current_pil))
        m.add_checkbutton(label="Show Name", variable=self.show_name, command=lambda: self.update_info_text(self.current_path, self.current_pil))
        m.add_checkbutton(label="Show Details", variable=self.show_details, command=lambda: self.update_info_text(self.current_path, self.current_pil))
        m.add_checkbutton(label="Show Stats", variable=self.show_stats, command=lambda: self.update_info_text(self.current_path, self.current_pil))
        m.tk_popup(e.x_root, e.y_root)

    def show_help(self):
        msg = """
        [Controls]
        Arrows: Nav
        Space: Pause
        Enter: Open Folder
        Z: Zoom Mode
        Shift (Hold): 2x Zoom
        Tab: Toolbar Lock
        F11: Fullscreen
        """
        tk.messagebox.showinfo("Help", msg)

    def open_current_folder(self):
        if not self.current_path: return
        p = self.current_path
        if VFS.is_virtual(p): p = VFS.get_parent(p) # Архив
        p = os.path.abspath(p)
        if os.name == 'nt': subprocess.run(['explorer', '/select,', p])
        elif platform.system() == 'Darwin': subprocess.run(['open', '-R', p])
        else: subprocess.run(['xdg-open', os.path.dirname(p)])

    def copy_path(self):
        if self.current_path:
            self.clipboard_clear()
            self.clipboard_append(self.current_path)

if __name__ == "__main__":
    app = SlideShowApp()
    if os.name == 'nt':
        try: app.state('zoomed')
        except: pass
    app.mainloop()

