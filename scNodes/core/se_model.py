from tensorflow.keras.callbacks import Callback
import tensorflow as tf
import tifffile
import numpy as np
from itertools import count
import glob
import os
import scNodes.core.config as cfg
import importlib
import threading
import json
from scNodes.core.opengl_classes import Texture
from scipy.ndimage import rotate
import datetime
from scipy.ndimage import zoom

# Note 230522: getting tensorflow to use the GPU is a pain. Eventually it worked with:
# Python 3.9, CUDA D11.8, cuDNN 8.6, tensorflow 2.8.0, protobuf 3.20.0, and adding
# LIBRARY_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v11.8\lib\x64 to the PyCharm run configuration environment variables.


class SEModel:
    idgen = count(0)
    AVAILABLE_MODELS = []
    MODELS = dict()
    MODELS_LOADED = False
    DEFAULT_COLOURS = [(66 / 255, 214 / 255, 164 / 255),
                       (255 / 255, 243 / 255, 0 / 255),
                       (255 / 255, 104 / 255, 0 / 255),
                       (255 / 255, 13 / 255, 0 / 255),
                       (174 / 255, 0 / 255, 255 / 255),
                       (21 / 255, 0 / 255, 255 / 255),
                       (0 / 255, 136 / 255, 266 / 255),
                       (0 / 255, 247 / 255, 255 / 255),
                       (0 / 255, 255 / 255, 0 / 255)]

    def __init__(self):
        uid_counter = next(SEModel.idgen)
        self.uid = int(datetime.datetime.now().strftime('%Y%m%d%H%M%S%f') + "000") + uid_counter
        self.title = "Unnamed model"
        self.colour = SEModel.DEFAULT_COLOURS[(uid_counter - 1) % len(SEModel.DEFAULT_COLOURS)]
        self.apix = -1.0
        self.compiled = False
        self.box_size = -1
        self.model = None
        self.model_enum = 3
        self.epochs = 25
        self.batch_size = 32
        self.train_data_path = "training_dataset.scnt"
        self.active = True
        self.export = True
        self.blend = False
        self.show = True
        self.alpha = 0.75
        self.threshold = 0.5
        self.overlap = 0.2
        self.active_tab = 0
        self.background_process_train = None
        self.background_process_apply = None
        self.n_parameters = 0
        self.n_copies = 4
        self.excess_negative = 30
        self.info = ""
        self.info_short = ""  ## TODO: add loss to info strings
        self.loss = 0.0
        self.data = None
        self.texture = Texture(format="r32f")
        self.texture.set_linear_mipmap_interpolation()

        if not SEModel.MODELS_LOADED:
            SEModel.load_models()

    def delete(self):
        pass

    def save(self, file_path):
        # Split the file_path into directory and file
        directory = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)

        # Save the Keras model
        model_path = os.path.join(directory, base_name + '_weights.h5')
        self.model.save(model_path)

        # Save metadata
        metadata = {
            'uid': self.uid,
            'title': self.title,
            'colour': self.colour,
            'apix': self.apix,
            'compiled': self.compiled,
            'box_size': self.box_size,
            'model_enum': self.model_enum,
            'epochs': self.epochs,
            'batch_size': self.batch_size,
            'active': self.active,
            'blend': self.blend,
            'show': self.show,
            'alpha': self.alpha,
            'threshold': self.threshold,
            'overlap': self.overlap,
            'active_tab': self.active_tab,
            'n_parameters': self.n_parameters,
            'n_copies': self.n_copies,
            'info': self.info,
            'info_short': self.info_short,
            'excess_negative': self.excess_negative
        }
        with open(file_path, 'w') as f:
            json.dump(metadata, f)

    def load(self, file_path):
        try:
            # Split the file_path into directory and file
            directory = os.path.dirname(file_path)
            base_name = os.path.basename(file_path)

            # Load the Keras model
            model_path = os.path.join(directory, base_name + '_weights.h5')
            self.model = tf.keras.models.load_model(model_path)

            # Load metadata
            with open(file_path, 'r') as f:
                metadata = json.load(f)
            self.uid = metadata['uid']
            self.title = metadata['title']
            self.colour = metadata['colour']
            self.apix = metadata['apix']
            self.compiled = metadata['compiled']
            self.box_size = metadata['box_size']
            self.model_enum = metadata['model_enum']
            self.epochs = metadata['epochs']
            self.batch_size = metadata['batch_size']
            self.active = metadata['active']
            self.blend = metadata['blend']
            self.show = metadata['show']
            self.alpha = metadata['alpha']
            self.threshold = metadata['threshold']
            self.overlap = metadata['overlap']
            self.active_tab = metadata['active_tab']
            self.n_parameters = metadata['n_parameters']
            self.n_copies = metadata['n_copies']
            self.info = metadata['info']
            self.info_short = metadata['info_short']
            self.excess_negative = metadata['excess_negative']
        except Exception as e:
            raise e
            print("Error loading model - see details below", print(e))

    def train(self):
        process = BackgroundProcess(self._train, (), name=f"{self.title} training")
        self.background_process_train = process
        process.start()

    def load_training_data(self):
        with tifffile.TiffFile(self.train_data_path) as train_data:
            train_data_apix = float(train_data.pages[0].description.split("=")[1])
            if self.apix == -1.0:
                self.apix = train_data_apix
            elif self.apix != train_data_apix:
                print(f"Note: the selected training data has a different pixel size {train_data_apix:.3f} than what the model was previously trained with ({self.apix:.3f}).")
        train_data = tifffile.imread(self.train_data_path)
        train_x = train_data[:, 0, :, :, None]
        train_y = train_data[:, 1, :, :, None]
        n_samples = train_x.shape[0]

        # split up the positive and negative indices
        positive_indices = list()
        negative_indices = list()
        for i in range(n_samples):
            if np.any(train_y[i]):
                positive_indices.append(i)
            else:
                negative_indices.append(i)

        n_pos = len(positive_indices)
        n_neg = len(negative_indices)
        positive_x = list()
        positive_y = list()
        for i in positive_indices:
            for _ in range(self.n_copies):
                if self.n_copies == 1:
                    norm_train_x = train_x[i] - np.mean(train_x[i])
                    denom = np.std(norm_train_x)
                    if denom != 0.0:
                        norm_train_x /= denom
                    positive_x.append(norm_train_x)
                    positive_y.append(train_y[i])
                else:
                    angle = np.random.uniform(0, 360)
                    x_rotated = rotate(train_x[i], angle, reshape=False, cval=np.mean(train_x[i]))
                    y_rotated = rotate(train_y[i], angle, reshape=False, cval=0.0)

                    x_rotated = (x_rotated - np.mean(x_rotated))
                    denom = np.std(x_rotated)
                    if denom != 0.0:
                        x_rotated /= np.std(x_rotated)

                    positive_x.append(x_rotated)
                    positive_y.append(y_rotated)

        if n_neg == 0:
            return np.array(positive_x), np.array(positive_y)

        extra_negative_factor = 1 + self.excess_negative / 100.0
        negative_sample_indices = negative_indices * int(extra_negative_factor * self.n_copies * n_pos // n_neg) + negative_indices[:int(extra_negative_factor * self.n_copies * n_pos) % n_neg]

        negative_x = list()
        negative_y = list()
        n_neg_copied = 0
        for i in negative_sample_indices:
            angle = np.random.uniform(0, 360)
            if self.n_copies == 1:
                angle = (n_neg_copied // n_neg) * 90.0
            x_rotated = rotate(train_x[i], angle, reshape=False, cval=np.mean(train_x[i]))

            x_rotated = (x_rotated - np.mean(x_rotated))
            denom = np.std(x_rotated)
            if denom != 0.0:
                x_rotated /= denom

            negative_x.append(x_rotated)
            negative_y.append(train_y[i])
            n_neg_copied += 1
        return np.array(positive_x + negative_x), np.array(positive_y + negative_y)

    def _train(self, process):
        try:
            train_x, train_y = self.load_training_data()
            n_samples = train_x.shape[0]
            box_size = train_x.shape[1]
            # compile, if not compiled yet
            if not self.compiled:
                self.compile(box_size)
            # if training data box size is not compatible with the compiled model, abort.
            if box_size != self.box_size:
                self.train_data_path = f"DATA HAS WRONG BOX SIZE ({box_size[0]} x {box_size[1]})"
                process.set_progress(1.0)
                return

            # train
            self.model.fit(train_x, train_y, epochs=self.epochs, batch_size=self.batch_size, shuffle=True, validation_split=0.1,
                           callbacks=[TrainingProgressCallback(process, n_samples, self.batch_size, self),
                                      StopTrainingCallback(process.stop_request)])
            process.set_progress(1.0)

        except Exception as e:
            cfg.set_error(e, "Could not train model - see details below.")
            process.stop()

    def reset_textures(self):
        pass

    def compile(self, box_size):
        model_module_name = SEModel.AVAILABLE_MODELS[self.model_enum]
        self.model = SEModel.MODELS[model_module_name]((box_size, box_size, 1))
        self.compiled = True
        self.box_size = box_size
        self.n_parameters = self.model.count_params()
        self.update_info()

    def update_info(self):
        self.info = SEModel.AVAILABLE_MODELS[self.model_enum] + f" ({self.n_parameters}, {self.box_size}, {self.apix:.3f}, {self.loss:.4f})"
        self.info_short = "(" + SEModel.AVAILABLE_MODELS[self.model_enum] + f", {self.box_size}, {self.apix:.3f}, {self.loss:.4f})"

    def set_slice(self, slice_data, slice_pixel_size, roi, original_size):
        if not self.compiled:
            return False
        if not self.active:
            return False
        self.data = np.zeros(original_size)
        rx, ry = roi
        self.data[rx[0]:rx[1], ry[0]:ry[1]] = self.apply_to_slice(slice_data[rx[0]:rx[1], ry[0]:ry[1]], slice_pixel_size)
        return True

    def update_texture(self):
        if not self.compiled or not self.active:
            return
        self.texture.update(self.data)

    def slice_to_boxes(self, image, pixel_size, as_array=True):
        scale_fac = pixel_size * 10.0 / self.apix
        image = zoom(image, scale_fac)
        w, h = image.shape
        self.overlap = min([0.9, self.overlap])
        pad_w = self.box_size - (w % self.box_size)
        pad_h = self.box_size - (h % self.box_size)
        # tile
        stride = int(self.box_size * (1.0 - self.overlap))
        boxes = list()
        image = np.pad(image, ((0, pad_w), (0, pad_h)))
        for x in range(0, w + pad_w - self.box_size + 1, stride):
            for y in range(0, h + pad_h - self.box_size + 1, stride):
                box = image[x:x + self.box_size, y:y + self.box_size]
                mu = np.mean(box, axis=(0, 1), keepdims=True)
                std = np.std(box, axis=(0, 1), keepdims=True)
                std[std == 0] = 1.0
                box = (box - mu) / std
                boxes.append(box)
        if as_array:
            boxes = np.array(boxes)
        return boxes, (w, h), (pad_w, pad_h), stride

    def boxes_to_slice(self, boxes, size, original_pixel_size, padding, stride):
        pad_w, pad_h = padding
        w, h = size
        out_image = np.zeros((w + pad_w, h + pad_h))
        count = np.zeros((w + pad_w, h + pad_h), dtype=int)
        i = 0
        for x in range(0, w + pad_w - self.box_size + 1, stride):
            for y in range(0, h + pad_h - self.box_size + 1, stride):
                out_image[x:x + self.box_size, y:y + self.box_size] += boxes[i]
                count[x:x + self.box_size, y:y + self.box_size] += 1
                i += 1
        count[count == 0] = 1
        out_image = out_image / count
        out_image = out_image[:w, :h]
        scale_fac = self.apix / (original_pixel_size * 10.0)
        out_image = zoom(out_image, scale_fac)
        return out_image[:w, :h]

    def apply_to_slice(self, image, pixel_size):
        # tile
        boxes, image_size, padding, stride = self.slice_to_boxes(image, pixel_size)
        # apply model
        seg_boxes = np.squeeze(self.model.predict(boxes))
        # detile
        segmentation = self.boxes_to_slice(seg_boxes, image_size, pixel_size, padding, stride)
        return segmentation

    def apply_to_multiple_slices(self, slice_list, pixel_size):
        all_boxes = list()
        n_boxes_per_slice = 0
        image_size = (0, 0)
        padding = (0, 0)
        stride = 0
        for image in slice_list:
            boxes, image_size, padding, stride = self.slice_to_boxes(image, pixel_size, as_array=False)
            n_boxes_per_slice = len(boxes)
            all_boxes += boxes
        all_boxes = np.array(all_boxes)
        seg_boxes = np.squeeze(self.model.predict(all_boxes))
        image_list = list()
        for i in range(len(slice_list)):
            image_seg_boxes = seg_boxes[i * n_boxes_per_slice: (i+1) * n_boxes_per_slice]
            image_list.append(self.boxes_to_slice(image_seg_boxes, image_size, pixel_size, padding, stride))
        return image_list

    @staticmethod
    def load_models():
        model_files = glob.glob(os.path.join(cfg.root, "models", "*.py"))
        for file in model_files:
            try:
                module_name = os.path.basename(file)[:-3]
                mod = importlib.import_module(("scNodes." if not cfg.frozen else "")+"models."+module_name)
                SEModel.MODELS[mod.title] = mod.create
            except Exception as e:
                cfg.set_error(e, "Could not load SegmentationEditor model at path: "+file)
        SEModel.MODELS_LOADED = True
        SEModel.AVAILABLE_MODELS = list(SEModel.MODELS.keys())

    def __eq__(self, other):
        if isinstance(other, SEModel):
            return self.uid == other.uid
        return False


class TrainingProgressCallback(Callback):
    def __init__(self, process, n_samples, batch_size, model):
        super().__init__()
        self.process = process
        self.batch_size = batch_size
        self.n_samples = n_samples
        self.batches_in_epoch = 0
        self.current_epoch = 0
        self.se_model = model

    def on_epoch_begin(self, epoch, logs=None):
        self.batches_in_epoch = self.n_samples // self.batch_size
        self.current_epoch = epoch

    def on_batch_end(self, batch, logs=None):
        progress_in_current_epoch = (batch + 1) / self.batches_in_epoch
        total_progress = (self.current_epoch + progress_in_current_epoch) / self.params['epochs']
        self.process.set_progress(total_progress)
        self.se_model.loss = logs['loss']
        self.se_model.update_info()


class StopTrainingCallback(Callback):
    def __init__(self, stop_request):
        super().__init__()
        self.stop_request = stop_request

    def on_batch_end(self, batch, logs=None):
        if self.stop_request.is_set():
            self.model.stop_training = True


class BackgroundProcess:
    idgen = count(0)

    def __init__(self, function, args, name=None):
        self.uid = next(BackgroundProcess.idgen)
        self.function = function
        self.args = args
        self.name = name
        self.thread = None
        self.progress = 0.0
        self.stop_request = threading.Event()

    def start(self):
        _name = f"BackgroundProcess {self.uid} - "+(self.name if self.name is not None else "")
        self.thread = threading.Thread(daemon=True, target=self._run, name=_name)
        self.thread.start()

    def _run(self):
        self.function(*self.args, self)

    def set_progress(self, progress):
        self.progress = progress

    def stop(self):
        self.stop_request.set()
        self.progress = 1.0

    def __str__(self):
        return f"BackgroundProcess {self.uid} with function {self.function} and args {self.args}"