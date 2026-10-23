"""固定对比样例（并非用户原始 163 行脚本）。仅作为审查输入，不执行。"""

import numpy as np
from keras import Sequential
from keras.layers import Dense, Input


def train():
    x = np.arange(1400, dtype=np.float32).reshape(700, 2)
    y = np.arange(700) % 2
    model = Sequential([Input(shape=(2,)), Dense(1, activation="sigmoid")])
    model.compile(optimizer="adam", loss="binary_crossentropy")
    model.fit(x, y, epochs=1, validation_split=0.2, shuffle=True)
    return model


def crop(frame):
    height, width = frame.shape[:2]
    box_size = 200
    x1, y1 = (width - box_size) // 2, (height - box_size) // 2
    x2, y2 = x1 + box_size, y1 + box_size
    roi = frame[y1:y2, x1:x2]
    return roi


def preview_overlay():
    frame = np.zeros((80, 90, 3), dtype=np.uint8)
    preview = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:100, :100] = preview
    return frame
