/* Custom fill-marker rendering for the price chart.
 *
 * Why not the built-in createSeriesMarkers(): its `text` is always drawn as
 * a separate element stacked outward from the shape, so a marker renders as
 * two vertically-stacked pieces (arrow + letter) that crowd each other and
 * the candle. This primitive draws each marker as ONE compact badge — a
 * rounded body with the label inside and a small tail pointing at the bar.
 *
 * Drawing happens inside the chart's own render loop, so badges stay
 * pixel-locked to the candles through pan/zoom with no separate sync code.
 *
 * One badge per candle per side: if the bot fills several times inside the
 * same candle, the extra fills are absorbed into that single badge with no
 * counter and no other visual trace.
 */

// Vertical clearance between a candle's high/low and the nearest edge of
// its badge, so badges never sit on the wick.
const BAR_GAP_PX = 7;

const BADGE_HEIGHT = 14;
const BADGE_PAD_X = 5;
const TAIL_HEIGHT = 4;
const TAIL_WIDTH = 6;
const BADGE_FONT = "600 10px -apple-system, BlinkMacSystemFont, sans-serif";
// Dark label on the saturated bull/bear fill reads better than white at 10px.
const LABEL_COLOR = "#0b0c0e";

function roundRectPath(ctx, x, y, w, h, r) {
  const rr = Math.min(r, h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.arcTo(x + w, y, x + w, y + rr, rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
  ctx.lineTo(x + rr, y + h);
  ctx.arcTo(x, y + h, x, y + h - rr, rr);
  ctx.lineTo(x, y + rr);
  ctx.arcTo(x, y, x + rr, y, rr);
  ctx.closePath();
}

function drawBadge(ctx, x, yAnchor, isBuy, label, color) {
  ctx.font = BADGE_FONT;
  const w = ctx.measureText(label).width + BADGE_PAD_X * 2;

  // dir = +1 when the badge sits below the bar (buy), -1 when above (sell).
  const dir = isBuy ? 1 : -1;
  const tailTipY = yAnchor + dir * BAR_GAP_PX;
  const bodyNearY = tailTipY + dir * TAIL_HEIGHT; // body edge closest to the bar
  const bodyTopY = isBuy ? bodyNearY : bodyNearY - BADGE_HEIGHT;
  const left = x - w / 2;

  ctx.fillStyle = color;
  roundRectPath(ctx, left, bodyTopY, w, BADGE_HEIGHT, 3);
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(x, tailTipY);
  ctx.lineTo(x - TAIL_WIDTH / 2, bodyNearY);
  ctx.lineTo(x + TAIL_WIDTH / 2, bodyNearY);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = LABEL_COLOR;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x, bodyTopY + BADGE_HEIGHT / 2 + 0.5);
}

class FillMarkersPaneView {
  constructor(source) {
    this._source = source;
  }

  update() {}

  zOrder() {
    return "top";
  }

  renderer() {
    const source = this._source;
    return {
      draw(target) {
        target.useMediaCoordinateSpace((scope) => {
          const ctx = scope.context;
          const series = source._series;
          const timeScale = source._chart ? source._chart.timeScale() : null;
          if (!series || !timeScale) return;

          for (const m of source._markers) {
            const candle = source._candles.get(m.time);
            if (!candle) continue;
            const x = timeScale.timeToCoordinate(m.time);
            if (x === null) continue;
            const isBuy = m.side === "buy";
            // Anchor to the candle's extreme on the side the badge sits, so
            // it never lands on top of the body or wick.
            const y = series.priceToCoordinate(isBuy ? candle.low : candle.high);
            if (y === null) continue;

            ctx.save();
            drawBadge(ctx, x, y, isBuy, isBuy ? "B" : "S", isBuy ? source._bull : source._bear);
            ctx.restore();
          }
        });
      },
    };
  }
}

class FillMarkersPrimitive {
  constructor({ bull, bear }) {
    this._bull = bull;
    this._bear = bear;
    this._markers = [];
    this._candles = new Map();
    this._paneView = new FillMarkersPaneView(this);
    this._series = null;
    this._chart = null;
    this._requestUpdate = null;
  }

  attached({ chart, series, requestUpdate }) {
    this._chart = chart;
    this._series = series;
    this._requestUpdate = requestUpdate;
  }

  detached() {
    this._requestUpdate = null;
  }

  updateAllViews() {}

  paneViews() {
    return [this._paneView];
  }

  setMarkers(markers) {
    this._markers = markers;
    this._redraw();
  }

  setCandles(candles) {
    this._candles = candles;
    this._redraw();
  }

  _redraw() {
    if (this._requestUpdate) this._requestUpdate();
  }
}
