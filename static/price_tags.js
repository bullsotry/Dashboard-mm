/* Price-line tags: resting orders and position entries, drawn CEX-style.
 *
 * Why not the built-in createPriceLine(): its axis label is a fixed shape
 * with no control over padding, radius or font, it cannot put two pieces of
 * text (label + price) in one tag, and — the reason that actually forces a
 * custom primitive — it has no collision handling. A market maker's orders
 * sit a few ticks apart, which is exactly the case where the built-in labels
 * land on top of each other and become unreadable.
 *
 * Layout rules, in priority order:
 *   1. The dashed line is ALWAYS at the true price. It is never displaced.
 *   2. The tag sits flush against the right edge, vertically centred on its
 *      line, so line and tag read as one object.
 *   3. Only when tags would overlap are they nudged apart, by the smallest
 *      total displacement that clears them, and a short leader is drawn from
 *      the tag back to its line so the pairing stays unambiguous.
 *
 * Read-only by design: there is deliberately no close/cancel affordance on
 * these tags. This dashboard cannot cancel an order, and a control that
 * looks like it can is worse than no control at all.
 */

const TAG_HEIGHT = 16;
const TAG_RADIUS = 2;
const TAG_PAD_X = 6;
const TAG_INNER_GAP = 7;
const TAG_STACK_GAP = 2; // vertical clearance between two stacked tags
const LABEL_FONT = "600 10px ui-monospace, Menlo, monospace";
const PRICE_FONT = "700 10.5px ui-monospace, Menlo, monospace";
// Dark text on the saturated bull/bear fill reads better than white at 10px,
// and matches the fill badges already on the chart.
const TAG_TEXT = "#0b0c0e";
const LINE_DASH_ORDER = [4, 3];

function tagRoundRect(ctx, x, y, w, h, r) {
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

/* Resolve overlaps with the smallest total movement: push the stack down
 * from the top, then, if it ran past the bottom, push it back up. Tags that
 * never collide keep their exact price position and are not touched. */
function layoutTags(items, viewportHeight) {
  const slot = TAG_HEIGHT + TAG_STACK_GAP;
  const sorted = items.slice().sort((a, b) => a.lineY - b.lineY);

  for (let i = 0; i < sorted.length; i++) {
    sorted[i].tagY = sorted[i].lineY;
    if (i > 0) sorted[i].tagY = Math.max(sorted[i].tagY, sorted[i - 1].tagY + slot);
  }
  const bottom = viewportHeight - TAG_HEIGHT / 2;
  const last = sorted[sorted.length - 1];
  if (last && last.tagY > bottom) {
    last.tagY = bottom;
    for (let i = sorted.length - 2; i >= 0; i--) {
      sorted[i].tagY = Math.min(sorted[i].tagY, sorted[i + 1].tagY - slot);
    }
    const top = TAG_HEIGHT / 2;
    if (sorted[0] && sorted[0].tagY < top) {
      // More tags than the viewport can stack. Clamping every one of them to
      // the top would silently pile them again, so let the excess run off
      // rather than draw a lie about how many distinct levels there are.
      sorted[0].tagY = top;
    }
  }
  return sorted;
}

function drawTag(ctx, item, rightEdge) {
  ctx.font = LABEL_FONT;
  const labelW = item.label ? ctx.measureText(item.label).width : 0;
  ctx.font = PRICE_FONT;
  const priceW = ctx.measureText(item.priceText).width;

  const innerGap = item.label ? TAG_INNER_GAP : 0;
  const w = TAG_PAD_X * 2 + labelW + innerGap + priceW;
  const x = rightEdge - w;
  const top = item.tagY - TAG_HEIGHT / 2;

  // Leader: only drawn when the tag had to move off its own line, so a
  // displaced tag still points unambiguously at the price it belongs to.
  if (Math.abs(item.tagY - item.lineY) > 0.5) {
    ctx.save();
    ctx.strokeStyle = item.color;
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(x - 0.5, Math.round(item.lineY) + 0.5);
    ctx.lineTo(x - 0.5, Math.round(item.tagY) + 0.5);
    ctx.stroke();
    ctx.restore();
  }

  ctx.fillStyle = item.color;
  tagRoundRect(ctx, x, top, w, TAG_HEIGHT, TAG_RADIUS);
  ctx.fill();

  ctx.fillStyle = TAG_TEXT;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const midY = top + TAG_HEIGHT / 2 + 0.5;
  let cursor = x + TAG_PAD_X;
  if (item.label) {
    ctx.font = LABEL_FONT;
    ctx.globalAlpha = 0.75; // label reads as secondary to the price
    ctx.fillText(item.label, cursor, midY);
    ctx.globalAlpha = 1;
    cursor += labelW + innerGap;
  }
  ctx.font = PRICE_FONT;
  ctx.fillText(item.priceText, cursor, midY);

  return x; // where the line must stop so it never runs under the tag
}

class PriceTagsPaneView {
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
          if (!series || source._tags.length === 0) return;

          const width = scope.mediaSize.width;
          const height = scope.mediaSize.height;

          const items = [];
          for (const t of source._tags) {
            const y = series.priceToCoordinate(t.price);
            if (y === null) continue;
            items.push({ ...t, lineY: y, tagY: y });
          }
          if (items.length === 0) return;

          const laid = layoutTags(items, height);

          ctx.save();
          for (const item of laid) {
            // Draw the tag first to learn its width, then run the line up to
            // its left edge — the line must not pass underneath the tag.
            const tagLeft = drawTag(ctx, item, width);

            ctx.save();
            ctx.strokeStyle = item.color;
            ctx.lineWidth = 1;
            ctx.globalAlpha = item.kind === "entry" ? 0.9 : 0.7;
            ctx.setLineDash(item.kind === "entry" ? [] : LINE_DASH_ORDER);
            ctx.beginPath();
            const lineY = Math.round(item.lineY) + 0.5;
            ctx.moveTo(0, lineY);
            ctx.lineTo(Math.max(0, tagLeft - 2), lineY);
            ctx.stroke();
            ctx.restore();
          }
          ctx.restore();
        });
      },
    };
  }
}

class PriceTagsPrimitive {
  constructor() {
    this._tags = [];
    this._paneView = new PriceTagsPaneView(this);
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

  /* tags: [{ price, priceText, label, color, kind: "order" | "entry" }] */
  setTags(tags) {
    this._tags = tags || [];
    if (this._requestUpdate) this._requestUpdate();
  }
}
