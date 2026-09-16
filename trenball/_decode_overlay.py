"""Draw the decoded grid position of every bead over a capture, for eyeballing."""

import sys

import cv2

from trenball.trend_decoder import EMPTY, decode_trend_grid, trim_grid_margins

LABEL_BGR = {"G": (0, 0, 0), "O": (0, 0, 0), "Y": (0, 0, 0)}


def main(src: str, dst: str) -> None:
    image = cv2.imread(src)
    trimmed = trim_grid_margins(image)
    decoded = decode_trend_grid(image, rows=5)

    h, w = trimmed.shape[:2]
    cell_w = w / float(decoded.cols)
    cell_h = h / float(decoded.rows)
    canvas = cv2.resize(trimmed, (w * 2, h * 2), interpolation=cv2.INTER_NEAREST)

    for c, column in enumerate(decoded.columns):
        for r, bead in enumerate(column):
            if bead == EMPTY:
                continue
            x = int((c + 0.5) * cell_w * 2)
            y = int((r + 0.5) * cell_h * 2)
            cv2.putText(
                canvas, f"{r}", (x - 6, y + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, LABEL_BGR[bead], 1, cv2.LINE_AA,
            )

    cv2.imwrite(dst, canvas)
    beads = sum(1 for col in decoded.columns for b in col if b != EMPTY)
    print(f"cols={decoded.cols} beads={beads} -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
