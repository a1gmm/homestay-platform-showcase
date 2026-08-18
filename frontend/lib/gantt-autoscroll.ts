/**
 * 甘特图拖拽换房时的「边缘自动滚动」。
 *
 * 背景：房态甘特用的是浏览器原生 HTML5 拖拽（draggable + onDragStart/onDragOver）。
 * 原生拖拽期间浏览器会屏蔽 wheel 事件——滚轮失灵，用户无法滚到当前视口外的房间，
 * 卡片就放不下去。补救办法是自定义边缘自动滚动：指针拖到滚动容器上/下边缘时，
 * 用 rAF 循环持续滚动容器（见 GanttView 里的接线）。
 *
 * 本文件只放纯计算函数，方便单测；DOM/rAF 副作用在组件里。
 */

export interface AutoScrollOptions {
  /** 上/下边缘触发区高度(px)，默认 64 */
  edgeSize?: number;
  /** 满速时每帧滚动像素，默认 18 */
  maxSpeed?: number;
}

/**
 * 计算容器纵向自动滚动速度。
 *
 * @param pointerY 指针在视口中的 clientY
 * @param rect     滚动容器的 getBoundingClientRect（只用 top / bottom）
 * @returns 每帧滚动像素：负=向上滚，正=向下滚，0=不滚。
 *          越靠近边缘越快（线性 ramp）；指针在容器外或处于中部时为 0。
 */
export function computeAutoScrollVelocity(
  pointerY: number,
  rect: { top: number; bottom: number },
  opts: AutoScrollOptions = {}
): number {
  const edgeSize = opts.edgeSize ?? 64;
  const maxSpeed = opts.maxSpeed ?? 18;

  const distFromTop = pointerY - rect.top;
  const distFromBottom = rect.bottom - pointerY;

  // 指针拖出容器上/下方 → 不滚，避免拖离后仍狂滚
  if (distFromTop < 0 || distFromBottom < 0) return 0;

  if (distFromTop < edgeSize) {
    const ratio = (edgeSize - distFromTop) / edgeSize; // 0..1，越靠上越大
    return -Math.ceil(ratio * maxSpeed);
  }
  if (distFromBottom < edgeSize) {
    const ratio = (edgeSize - distFromBottom) / edgeSize;
    return Math.ceil(ratio * maxSpeed);
  }
  return 0;
}
