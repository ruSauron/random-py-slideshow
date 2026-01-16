import os
import sys
import time
import random
import threading
import subprocess
import argparse
import tkinter as tk
from tkinter import ttk, messagebox, Menu
from collections import deque
from pathlib import Path

# --- КОНФИГУРАЦИЯ ---
CFG_SLIDE_DURATION = 4.0      # Время показа слайда (сек)
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.webp', '.ico', '.tiff'}
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100 # Высота зоны снизу для активации панели
CFG_TOOLBAR_HEIGHT = 40

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ---
class Utils:
    @staticmethod
    def format_size(size_bytes):
        """Форматирует байты в читаемый вид (KB, MB, GB)."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

class ToolTip:
    """Всплывающие подсказки при наведении мыши."""
    def __init__(self, widget, text, delay_ms=500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress>", self._hide, add=True)

    def _schedule(self, _=None):
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip or not self.text: return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self._tip, text=self.text, bg="#111111", fg="#eeeeee",
                       relief="solid", borderwidth=1, font=("Segoe UI", 8))
        lbl.pack(ipadx=6, ipady=3)

    def _hide(self, _=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

# --- ЗАГРУЗЧИК ИЗОБРАЖЕНИЙ ---
try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    messagebox.showerror("Error", "Pillow library not found.\nPlease install it: pip install Pillow")
    sys.exit(1)

class ImageLoader:
    def __init__(self):
        # Кэш только для ТЕКУЩЕГО файла.
        self.current_file_cache = {} 
        self.last_path = None
        self.lock = threading.Lock()
        self.current_screen_size = (1920, 1080)

    def update_screen_size(self, width, height):
        self.current_screen_size = (width, height)

    def load_image(self, path, fit_mode, rotation):
        with self.lock:
            # Сброс кэша при смене файла
            if path != self.last_path:
                self.current_file_cache.clear()
                self.last_path = path

            # Ключ уникальности картинки
            cache_key = (fit_mode, rotation, self.current_screen_size)
            
            if cache_key in self.current_file_cache:
                return self.current_file_cache[cache_key]

        try:
            safe_path = str(Path(path).resolve())
            if os.name == 'nt' and not safe_path.startswith('\\\\?\\'):
                safe_path = '\\\\?\\' + safe_path

            img = Image.open(safe_path)
            img = ImageOps.exif_transpose(img) 
            
            if rotation != 0:
                img = img.rotate(rotation, expand=True)

            sw, sh = self.current_screen_size
            iw, ih = img.size
            target_w, target_h = iw, ih

            # Расчет целевых размеров
            if fit_mode == 0: # Fit (Вписать)
                ratio = min(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
            elif fit_mode == 1: # Original
                pass
            elif fit_mode == 2: # Fill (Заполнить)
                ratio = max(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
            elif fit_mode == 3: # 4x Zoom (Лупа)
                ratio = min(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio * 4), int(ih * ratio * 4)

            if target_w < 1: target_w = 1
            if target_h < 1: target_h = 1

            # Оптимизация
            if (target_w, target_h) != (iw, ih):
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            
            tk_img = ImageTk.PhotoImage(img)

            with self.lock:
                self.current_file_cache[cache_key] = (img, tk_img)
            
            return img, tk_img
        except Exception as e:
            print(f"Error loading {path}: {e}")
            return None, None

# --- ОСНОВНОЕ ПРИЛОЖЕНИЕ ---
class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()
        self.title("Fast Random PySlideshow")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)
        
        # --- Структуры данных ---
        self.all_files = []      # Глобальный список всех найденных файлов
        self.folder_set = set()  # Уникальные папки
        self.unviewed_indices = [] # Индексы в all_files
        
        # Защита от повторов: храним пути, которые мы УЖЕ показали.
        self.viewed_paths = set() 
        
        self.history = deque(maxlen=500) # История
        self.history_pointer = -1 
        
        self.current_path = None
        self.current_file_index = -1
        
        # Состояние плеера
        self.is_paused = False
        self.slide_timer = None
        self.is_scanning_active = True 

        # Настройки просмотра
        self.zoom_mode = 0 # 0=Fit, 1=Orig, 2=Fill
        self.temp_zoom = False
        self.rotation = 0
        
        # Настройки инфо-панели
        self.show_path = tk.BooleanVar(value=True)
        self.show_name = tk.BooleanVar(value=True)
        self.show_details = tk.BooleanVar(value=True)
        self.show_stats = tk.BooleanVar(value=True)
        
        # UI
        self.toolbar_locked = True
        self.fullscreen = False
        self.w_state_before_full = 'zoomed'
        self.was_locked_before_fs = True
        self.image_shown_flag = False 

        self.loader = ImageLoader()
        
        self.setup_ui()
        self.bind_events()
        
        self.start_initial_search()
        
        if self.cli_args.fullscreen:
            self.toggle_fullscreen()

    def parse_cli_args(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("path", nargs="?", default=None, help="Root folder")
        parser.add_argument("--cwd", action="store_true", help="Current directory")
        parser.add_argument("--fullscreen", action="store_true", help="Start fullscreen")
        self.cli_args = parser.parse_args()

        if self.cli_args.cwd:
            self.root_dir = os.getcwd()
        elif self.cli_args.path:
            self.root_dir = os.path.abspath(self.cli_args.path)
        else:
            self.root_dir = str(Path(__file__).resolve().parent)

    def setup_ui(self):
        self.canvas = tk.Canvas(self, bg=CFG_BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        self.toolbar = tk.Frame(self, bg="#333333", height=CFG_TOOLBAR_HEIGHT)
        self.toolbar.pack_propagate(False)
        self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TButton", font=("Segoe UI", 9), padding=2)

        def btn(text, cmd, width=None, tooltip=""):
            b = ttk.Button(self.toolbar, text=text, command=cmd, width=width)
            b.pack(side='left', padx=2)
            if tooltip: ToolTip(b, tooltip)
            return b

        # Кнопки панели
        btn("?", self.show_help, 2, "Help (F1)")
        self.btn_lock = btn("FIX", self.toggle_toolbar_lock, 6, "Lock Toolbar")
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5, "Fullscreen (F11)")
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 10, "Zoom Mode (Z)")
        
        btn("CCR", lambda: self.rotate_image(-90), 4, "Rotate Left")
        btn("CR", lambda: self.rotate_image(90), 4, "Rotate Right")

        btn("<--", self.prev_image, 4, "Previous (Left Arrow)")
        btn("-->", self.next_image, 4, "Next Random (Right Arrow)")
        
        btn("<<", self.first_file_folder, 3, "First in Folder")
        btn("<-", self.prev_file_alpha, 3, "Prev File (Up)")
        btn("->", self.next_file_alpha, 3, "Next File (Down)")

        self.btn_play = btn("PAUSE", self.toggle_pause, 6, "Play/Pause (Space)")

        tk.Label(self.toolbar, text="Sec:", bg="#333333", fg="white", font=("Segoe UI", 9)).pack(side='left', padx=(5,0))
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION))
        self.speed_var.trace("w", self.on_speed_change)
        tk.Entry(self.toolbar, textvariable=self.speed_var, width=4, font=("Segoe UI", 9)).pack(side='left', padx=2)

        btn("FOLDER", self.open_current_folder, 7, "Show in Explorer")

        self.lbl_info = tk.Label(self.toolbar, text="Initializing...", bg="#333333", fg=CFG_TEXT_COLOR, 
                                 font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_preset())
        self.lbl_info.bind("<Button-3>", self.show_info_menu)

    def bind_events(self):
        self.bind("<Right>", lambda e: self.next_image())
        self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<space>", lambda e: self.toggle_pause())
        
        # Навигация внутри папки
        self.bind("<Up>", lambda e: self.prev_file_alpha())    
        self.bind("<Down>", lambda e: self.next_file_alpha())  
        self.bind("<Home>", lambda e: self.first_file_folder())
        
        # Навигация ПО ПАПКАМ
        self.bind("<Prior>", lambda e: self.nav_folder_prev()) # PgUp
        self.bind("<Next>", lambda e: self.nav_folder_next())  # PgDn
        
        self.bind("<Return>", lambda e: self.open_current_folder())
        
        self.bind("<F1>", lambda e: self.show_help())
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Alt-Return>", lambda e: self.toggle_fullscreen())
        
        self.bind("z", lambda e: self.cycle_zoom())
        self.bind("Z", lambda e: self.cycle_zoom())
        self.bind("<Shift_L>", self.enable_temp_zoom)
        self.bind("<KeyRelease-Shift_L>", self.disable_temp_zoom)
        self.bind("<Shift_R>", self.enable_temp_zoom)
        self.bind("<KeyRelease-Shift_R>", self.disable_temp_zoom)
        
        self.bind("<Control-r>", lambda e: self.rotate_image(90))
        self.bind("<Control-e>", lambda e: self.rotate_image(-90))
        self.bind("i", lambda e: self.cycle_info_preset())
        self.bind("I", lambda e: self.cycle_info_preset())
        
        self.canvas.bind("<Motion>", self.check_toolbar_hover)
        self.bind("<Tab>", lambda e: self.toggle_toolbar_lock())
        
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Motion>", self.on_canvas_motion, add=True)
        self.bind("<Configure>", self.on_resize)

    # --- ЛОГИКА СКАНИРОВАНИЯ ---
    
    def start_initial_search(self):
        threading.Thread(target=self.find_first_image_task, daemon=True).start()

    def find_first_image_task(self):
        self.find_random_image_dynamic(initial=True)
        time.sleep(0.5)
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def find_random_image_dynamic(self, initial=False):
        try:
            current = self.root_dir
            for i in range(50):
                if initial and self.image_shown_flag: return 
                
                # Показываем активность, если еще ничего не нашли
                if initial and i % 5 == 0:
                     self.lbl_info.config(text=f"Scanning: {os.path.basename(current)}...")

                try:
                    entries = list(os.scandir(current))
                except (OSError, PermissionError):
                    break 
                
                dirs = [e.path for e in entries if e.is_dir()]
                files = [e.path for e in entries if e.is_file() 
                         and os.path.splitext(e.name)[1].lower() in CFG_EXTENSIONS]
                
                unseen_files = [f for f in files if f not in self.viewed_paths]
                
                pick_here = False
                if unseen_files:
                    if not dirs: pick_here = True
                    elif random.random() < 0.25: pick_here = True
                
                if pick_here and unseen_files:
                    pick = random.choice(unseen_files)
                    self.after(0, lambda p=pick: self.load_dynamic_result(p, initial))
                    return
                
                if dirs:
                    current = random.choice(dirs)
                else:
                    break
        except Exception as e:
            print(f"Walker error: {e}")

    def load_dynamic_result(self, path, initial):
        if initial and self.image_shown_flag: return
        if path in self.viewed_paths: return

        self.load_by_path(path)
        
        # Добавляем первую картинку в историю
        if not self.history:
            self.history.append(path)
            self.history_pointer = 0
            
        if not self.is_paused:
            self.schedule_next_slide()

    def scan_worker(self):
        temp_batch = []
        last_update = time.time()
        
        for root, dirs, files in os.walk(self.root_dir):
            random.shuffle(dirs)
            
            for f in files:
                if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS:
                    full_path = os.path.join(root, f)
                    temp_batch.append(full_path)
            
            if len(temp_batch) > 1000 or (time.time() - last_update > 0.5 and temp_batch):
                self.after(0, lambda b=list(temp_batch): self.add_files_batch(b))
                temp_batch = []
                last_update = time.time()
        
        if temp_batch:
            self.after(0, lambda b=list(temp_batch): self.add_files_batch(b))
            
        self.is_scanning_active = False

    def add_files_batch(self, batch):
        start_idx = len(self.all_files)
        self.all_files.extend(batch)
        new_indices = list(range(start_idx, start_idx + len(batch)))
        self.unviewed_indices.extend(new_indices)
        
        for p in batch:
            self.folder_set.add(os.path.dirname(p))
            
        if self.image_shown_flag and self.show_stats.get():
             self.display_current_image()

    # --- ЛОГИКА НАВИГАЦИИ ---

    def get_random_index(self):
        if not self.all_files: return -1
        if not self.unviewed_indices:
             self.unviewed_indices = list(range(len(self.all_files)))
        
        while self.unviewed_indices:
            rnd_idx = random.randrange(len(self.unviewed_indices))
            val = self.unviewed_indices[rnd_idx]
            
            self.unviewed_indices[rnd_idx] = self.unviewed_indices[-1]
            self.unviewed_indices.pop()
            
            path = self.all_files[val]
            if path in self.viewed_paths: continue
            return val
        return -1

    def goto_index(self, index, record_history=True):
        if index < 0 or index >= len(self.all_files): return
        path = self.all_files[index]
        self.load_by_path(path, index)
        if record_history:
            self.history.append(path)
            self.history_pointer = len(self.history) - 1

    def next_image(self):
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            path = self.history[self.history_pointer]
            self.load_by_path(path)
            return

        if self.is_scanning_active and len(self.all_files) < 2000:
             threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()
        else:
            idx = self.get_random_index()
            if idx != -1:
                self.goto_index(idx, True)
            else:
                threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()

    def prev_image(self):
        if self.history_pointer > 0:
            self.history_pointer -= 1
            path = self.history[self.history_pointer]
            self.load_by_path(path)

    def load_by_path(self, path, known_index=-1):
        self.current_path = path
        self.viewed_paths.add(path)
        
        if known_index != -1:
            self.current_file_index = known_index
        else:
            try:
                self.current_file_index = self.all_files.index(path)
            except ValueError:
                self.current_file_index = -1
        
        self.rotation = 0
        self.display_current_image()
        self.reset_timer()

    # --- Навигация внутри папки ---
    def nav_sibling(self, offset):
        if not self.current_path: return
        parent = os.path.dirname(self.current_path)
        try:
            files = sorted([os.path.join(parent, f) for f in os.listdir(parent) 
                            if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS])
            if not files: return
            try:
                curr_idx = files.index(self.current_path)
                next_idx = (curr_idx + offset) % len(files)
                path = files[next_idx]
                self.load_by_path(path)
                
                # Добавляем в историю (чтобы можно было вернуться влево)
                if not self.history or self.history[-1] != path:
                    self.history.append(path)
                    self.history_pointer = len(self.history) - 1
            except ValueError:
                if files: self.load_by_path(files[0])
        except OSError: pass

    def next_file_alpha(self): self.nav_sibling(1)
    def prev_file_alpha(self): self.nav_sibling(-1)
    
    def first_file_folder(self):
        if not self.current_path: return
        parent = os.path.dirname(self.current_path)
        try:
            files = sorted([os.path.join(parent, f) for f in os.listdir(parent) 
                            if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS])
            if files: self.load_by_path(files[0])
        except OSError: pass

    # --- Навигация ПО ПАПКАМ (PgUp/PgDn) ---
    def nav_folder_step(self, offset):
        if not self.current_path: return
        
        # Стратегия 1: Глобальная навигация (если скан завершен)
        # Позволяет ходить по всем папкам, даже вложенным
        if not self.is_scanning_active and self.folder_set:
            current_folder = os.path.dirname(self.current_path)
            sorted_folders = sorted(list(self.folder_set))
            try:
                idx = sorted_folders.index(current_folder)
                next_idx = (idx + offset) % len(sorted_folders)
                target_folder = sorted_folders[next_idx]
                self._load_random_in_folder(target_folder)
            except ValueError:
                # Если текущая папка не в списке (странно, но бывает), пробуем локальный метод
                self._nav_folder_local(offset)
        else:
            # Стратегия 2: Локальная навигация (во время сканирования)
            # Смотрим только соседей в родительской папке
            self._nav_folder_local(offset)

    def _nav_folder_local(self, offset):
        """Переход к соседней папке на уровне файловой системы."""
        try:
            current_folder = os.path.dirname(self.current_path)
            parent_folder = os.path.dirname(current_folder)
            
            # Получаем список соседних папок
            entries = list(os.scandir(parent_folder))
            subdirs = sorted([e.path for e in entries if e.is_dir()])
            
            if not subdirs: return

            # Ищем индекс текущей
            try:
                # Важно: нормализуем пути для точного сравнения
                norm_curr = os.path.normpath(current_folder)
                norm_subdirs = [os.path.normpath(p) for p in subdirs]
                idx = norm_subdirs.index(norm_curr)
            except ValueError:
                idx = 0
            
            # Ищем следующую папку, в которой ЕСТЬ картинки
            # Ограничиваем круг поиска, чтобы не зависнуть если все папки пустые
            for _ in range(len(subdirs)):
                idx = (idx + offset) % len(subdirs)
                target_folder = subdirs[idx]
                
                # Пробуем загрузить что-то из этой папки
                if self._load_random_in_folder(target_folder):
                    return
                    
        except OSError:
            pass

    def _load_random_in_folder(self, folder):
        """Пытается найти и загрузить случайную картинку в указанной папке. Возвращает успех."""
        try:
            # Быстрый листинг файлов
            files = [f for f in os.listdir(folder) 
                     if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS]
            
            if files:
                target_file = os.path.join(folder, random.choice(files))
                self.load_by_path(target_file)
                
                # Добавляем в историю
                if not self.history or self.history[-1] != target_file:
                    self.history.append(target_file)
                    self.history_pointer = len(self.history) - 1
                return True
        except OSError:
            pass
        return False

    def nav_folder_next(self): self.nav_folder_step(1)
    def nav_folder_prev(self): self.nav_folder_step(-1)

    # --- ОТРИСОВКА ---

    def display_current_image(self):
        if not self.current_path: return
        self.image_shown_flag = True
        
        mode = 3 if self.temp_zoom else self.zoom_mode
        self.update_loader_dims()
        pil_img, tk_img = self.loader.load_image(self.current_path, mode, self.rotation)
        
        # FIX 1: Даже при ошибке обновляем UI, чтобы показать путь
        if not pil_img:
            self.canvas.delete("all")
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, 
                                    text="Error loading image", fill="white")
            self.update_info_label(None) # Передаем None вместо img_obj
            return
            
        self.current_tk_image = tk_img
        self.canvas.delete("all")
        
        cx, cy = self.winfo_width()//2, self.winfo_height()//2
        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags='img')
        
        if mode == 3:
            self.update_zoom_pan()
            
        self.update_info_label(pil_img)

    def update_info_label(self, img_obj):
        if not self.current_path:
            self.lbl_info.config(text="")
            return

        parts = []
        if self.show_path.get(): parts.append(os.path.dirname(self.current_path))
        if self.show_name.get(): parts.append(os.path.basename(self.current_path))
            
        if self.show_details.get():
            try:
                stats = os.stat(self.current_path)
                f_size = Utils.format_size(stats.st_size)
                
                # Если картинка битая (img_obj=None), не показываем разрешение
                if img_obj:
                    res = f"{img_obj.width}x{img_obj.height}"
                    parts.append(f"[{res}] [{f_size}]")
                else:
                    parts.append(f"[???] [{f_size}]")
            except: pass
            
        if self.show_stats.get():
            viewed = len(self.viewed_paths)
            total = len(self.all_files)
            folders = len(self.folder_set)
            if total < viewed: total = viewed 
            parts.append(f"({viewed} of {total} in {folders})")

        self.lbl_info.config(text=" ".join(parts))

    def on_resize(self, event):
        if hasattr(self, '_resize_job'):
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self.display_current_image)

    def update_loader_dims(self):
        self.loader.update_screen_size(self.winfo_width(), self.winfo_height())

    # --- ЗУМ И ПАН ---
    
    def cycle_zoom(self):
        modes = ["ZOOM Fit", "ZOOM Orig", "ZOOM Fill"]
        self.zoom_mode = (self.zoom_mode + 1) % 3
        self.btn_zoom.config(text=modes[self.zoom_mode])
        self.display_current_image()
        self.reset_timer() 

    def enable_temp_zoom(self, event):
        if not self.temp_zoom:
            self.temp_zoom = True
            self.display_current_image()
            self.reset_timer() 

    def disable_temp_zoom(self, event):
        if self.temp_zoom:
            self.temp_zoom = False
            self.display_current_image()
            self.reset_timer() 

    def on_canvas_motion(self, event):
        mode = 3 if self.temp_zoom else self.zoom_mode
        if mode == 3 and hasattr(self, 'current_tk_image'):
            w, h = self.winfo_width(), self.winfo_height()
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            
            cx, cy = w/2, h/2
            if iw > w:
                ratio_x = event.x / w
                img_left = - (iw - w) * ratio_x
                cx = img_left + iw/2
            if ih > h:
                ratio_y = event.y / h
                img_top = - (ih - h) * ratio_y
                cy = img_top + ih/2
            self.canvas.coords('img', cx, cy)

    def update_zoom_pan(self):
        x = self.winfo_pointerx() - self.winfo_rootx()
        y = self.winfo_pointery() - self.winfo_rooty()
        class E: pass
        e = E()
        e.x, e.y = x, y
        self.on_canvas_motion(e)

    def rotate_image(self, deg):
        self.rotation = (self.rotation - deg) % 360
        self.display_current_image()
        self.reset_timer()

    # --- ВЗАИМОДЕЙСТВИЕ ---

    def show_help(self):
        text = """
        KEYBOARD SHORTCUTS
        [Navigation]
        Right Arrow : Next Random Image
        Left Arrow  : Previous Image (History)
        Space       : Play / Pause
        Enter       : Open File Location
        
        [Folder Navigation]
        PgDn        : Next Folder (Random image inside)
        PgUp        : Prev Folder (Random image inside)
        Down Arrow  : Next File in current folder
        Up Arrow    : Prev File in current folder
        Home        : First File in Folder
        
        [View]
        Z           : Cycle Zoom (Fit/Orig/Fill)
        Shift (Hold): Temporary 4x Zoom (Loupe)
        Ctrl+R      : Rotate Clockwise
        Ctrl+E      : Rotate Counter-Clockwise
        I           : Cycle Info Modes (Right click for menu)
        
        [Window]
        Tab            : Show / Hide Toolbar
        F11 / Alt+Enter: Fullscreen
        Esc            : Exit Fullscreen / Quit
        F1             : This Help

        [Command Line]
        path           : Start scanning from specific folder
        --cwd          : Start in current working directory
        --fullscreen   : Start in fullscreen mode
        """
        messagebox.showinfo("Help", text)

    def show_context_menu(self, event):
        m = Menu(self, tearoff=0)
        m.add_command(label="Next Random", command=self.next_image)
        m.add_command(label="Play/Pause", command=self.toggle_pause)
        m.add_separator()
        m.add_command(label="Open Folder", command=self.open_current_folder)
        m.add_command(label="Toggle Fullscreen", command=self.toggle_fullscreen)
        m.tk_popup(event.x_root, event.y_root)

    def show_info_menu(self, event):
        m = Menu(self, tearoff=0)
        m.add_checkbutton(label="File Name", variable=self.show_name, command=self.update_info_label_wrapper)
        m.add_checkbutton(label="Folder Path", variable=self.show_path, command=self.update_info_label_wrapper)
        m.add_checkbutton(label="Resolution & Size", variable=self.show_details, command=self.update_info_label_wrapper)
        m.add_checkbutton(label="Statistics", variable=self.show_stats, command=self.update_info_label_wrapper)
        m.tk_popup(event.x_root, event.y_root)

    def update_info_label_wrapper(self):
        self.display_current_image()

    def cycle_info_preset(self):
        s_n = self.show_name.get()
        s_p = self.show_path.get()
        s_d = self.show_details.get()
        
        if s_n and s_p and s_d: 
            self.show_path.set(False); self.show_details.set(False); self.show_stats.set(False)
            self.show_name.set(True)
        elif s_n and not s_p and not s_d: 
            self.show_path.set(True); self.show_name.set(True)
        elif s_n and s_p and not s_d: 
            self.show_path.set(False); self.show_name.set(False)
        else: 
            self.show_path.set(True); self.show_name.set(True)
            self.show_details.set(True); self.show_stats.set(True)
        self.display_current_image()

    def check_toolbar_hover(self, event):
        if self.toolbar_locked: return
        root_y = self.winfo_rooty()
        pointer_y = self.winfo_pointery()
        if pointer_y < root_y or pointer_y > root_y + self.winfo_height(): return
        
        rel_y = pointer_y - root_y
        if self.winfo_height() - rel_y < CFG_TOOLBAR_TRIGGER_ZONE:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        else:
            self.toolbar.place(relx=0, rely=1.0, y=100, anchor='sw', relwidth=1.0)

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def toggle_fullscreen(self, force_exit=False):
        if force_exit:
            self.fullscreen = False
        else:
            self.fullscreen = not self.fullscreen
            
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

    def on_speed_change(self, *args):
        try:
            val = self.speed_var.get().replace(',', '.')
            if float(val) <= 0: raise ValueError
        except ValueError:
            self.is_paused = True
            self.btn_play.config(text="PLAY")

    def schedule_next_slide(self):
        if self.slide_timer: self.after_cancel(self.slide_timer)
        if self.is_paused: return
        try:
            val = self.speed_var.get().replace(',', '.')
            sec = float(val)
        except ValueError: sec = 4.0
        self.slide_timer = self.after(int(sec * 1000), self.auto_next)

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
        p = os.path.normpath(self.current_path)
        try:
            if os.name == 'nt': subprocess.run(['explorer', '/select,', p])
            else: subprocess.run(['xdg-open', os.path.dirname(p)])
        except Exception as e: print(e)

if __name__ == "__main__":
    app = SlideShowApp()
    if os.name == 'nt':
        try: app.state('zoomed')
        except: pass
    else:
        app.attributes('-zoomed', True)
    app.mainloop()
