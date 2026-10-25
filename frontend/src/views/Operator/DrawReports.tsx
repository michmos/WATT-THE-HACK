// Operator-drawn report areas: Ctrl + press-and-hold + drag on the map draws a freehand stroke that is closed and
// simplified into a polygon; a small form then asks for one or more fault types, a confidence weight and an optional
// description. The result is
// stored as an ordinary Report (id "drawn_N"), so heatmap, risk and simulation treat it like an imported one.
import { useEffect, useRef, useState } from 'react'
import { Layer, Source, useMap } from 'react-map-gl/maplibre'
import type { Feature, FeatureCollection, Polygon, Position } from 'geojson'
import type { FaultType, Report } from '../../api'
import { FAULT_STYLE } from '../SimSetup/SimSetup'

export const DRAWN_PREFIX = 'drawn_'
export const isDrawn = (r: Report) => r.report_id.startsWith(DRAWN_PREFIX)

const FAULT_TYPES = Object.keys(FAULT_STYLE) as FaultType[]

/** The polygon being created, or the drawn report being edited (id set). */
export interface Draft {
  id?: string
  polygon: Polygon
  faultTypes: FaultType[]
  confidence: number
  text: string
}

export const nextDrawnId = (reports: Report[]) =>
  `${DRAWN_PREFIX}${Math.max(0, ...reports.filter(isDrawn).map((r) => Number(r.report_id.slice(DRAWN_PREFIX.length)) || 0)) + 1}`

export const draftToReport = (d: Draft, id: string): Report => ({
  report_id: id,
  polygon: d.polygon,
  fault_types: d.faultTypes,
  confidence: d.confidence,
  text: d.text.trim(),
})

// ---------- geometry ----------

function distToSegment(p: Position, a: Position, b: Position) {
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const len2 = dx * dx + dy * dy
  const t = len2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2)) : 0
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))
}

/** Ramer-Douglas-Peucker on an open polyline. */
function simplify(pts: Position[], tol: number): Position[] {
  if (pts.length < 3) return pts
  let worst = 0
  let at = 0
  for (let i = 1; i < pts.length - 1; i++) {
    const d = distToSegment(pts[i], pts[0], pts[pts.length - 1])
    if (d > worst) [worst, at] = [d, i]
  }
  if (worst <= tol) return [pts[0], pts[pts.length - 1]]
  return [...simplify(pts.slice(0, at + 1), tol).slice(0, -1), ...simplify(pts.slice(at), tol)]
}

const cross = (a: Position, b: Position, c: Position) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
const segmentsCross = (a: Position, b: Position, c: Position, d: Position) =>
  cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0

/** true if any two non-adjacent edges of the closed ring cross (ring without repeated last point) */
function selfIntersects(ring: Position[]) {
  const n = ring.length
  for (let i = 0; i < n; i++) {
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue
      if (segmentsCross(ring[i], ring[(i + 1) % n], ring[j], ring[(j + 1) % n])) return true
    }
  }
  return false
}

/** Closed, simplified polygon from a freehand stroke, or an error message. */
function strokeToPolygon(stroke: Position[], tolDeg: number): Polygon | string {
  const ring = simplify(stroke, tolDeg)
  if (ring.length < 3) return 'Stroke too short to make an area.'
  if (selfIntersects(ring)) return 'The outline crosses itself. Draw it again without crossing.'
  const area = Math.abs(ring.reduce((s, p, i) => s + cross(ring[0], p, ring[(i + 1) % ring.length]), 0)) / 2
  if (area < tolDeg * tolDeg) return 'Area too small.'
  return { type: 'Polygon', coordinates: [[...ring, ring[0]]] }
}

// ---------- map layer: gesture + preview ----------

export function DrawLayer({
  mapId,
  enabled,
  draft,
  onDrawn,
  onCancel,
  onError,
}: {
  mapId: string
  enabled: boolean
  draft: Draft | null
  onDrawn: (polygon: Polygon) => void
  onCancel: () => void
  onError: (message: string) => void
}) {
  const { [mapId]: ref } = useMap()
  const [stroke, setStroke] = useState<Position[]>([])
  const cb = useRef({ onDrawn, onCancel, onError })
  cb.current = { onDrawn, onCancel, onError }

  useEffect(() => {
    const m = ref?.getMap()
    if (!m || !enabled) return
    const map = m
    const canvas = map.getCanvasContainer()
    let pts: Position[] | null = null
    // the browser still fires a click after the stroke; it must not reach the map's click handlers (dead-line toggle)
    let swallowClick = false
    // offsetX/Y would be relative to a marker element when the cursor is over one; use the canvas rect instead
    const lngLat = (e: MouseEvent): Position => {
      const r = map.getCanvas().getBoundingClientRect()
      const p = map.unproject([e.clientX - r.left, e.clientY - r.top])
      return [p.lng, p.lat]
    }

    const onUp = () => finish(true)
    function finish(commit: boolean) {
      if (!pts) return
      const done = pts
      pts = null
      setStroke([])
      map.dragPan.enable()
      window.removeEventListener('mouseup', onUp)
      if (!commit || done.length < 3) return // Ctrl + plain click: nothing drawn
      // ~4 screen pixels in degrees at the current zoom
      const tol = (4 * 360) / (512 * 2 ** map.getZoom())
      const res = strokeToPolygon(done, tol)
      if (typeof res === 'string') cb.current.onError(res)
      else cb.current.onDrawn(res)
    }
    const onDown = (e: MouseEvent) => {
      swallowClick = false // a stroke released outside the map produces no click to swallow
      if (!e.ctrlKey || e.button !== 0) return
      e.preventDefault()
      e.stopPropagation()
      map.dragPan.disable()
      swallowClick = true
      pts = [lngLat(e)]
      setStroke(pts)
      window.addEventListener('mouseup', onUp)
    }
    const onMove = (e: MouseEvent) => {
      if (!pts) return
      pts = [...pts, lngLat(e)]
      setStroke(pts)
    }
    const onClick = (e: MouseEvent) => {
      if (!swallowClick) return
      swallowClick = false
      e.stopPropagation()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (pts) finish(false)
      else cb.current.onCancel()
    }
    // capture phase: runs before MapLibre's own handlers so a Ctrl-drag never starts a pan or rotate
    canvas.addEventListener('mousedown', onDown, true)
    canvas.addEventListener('click', onClick, true)
    canvas.addEventListener('mousemove', onMove)
    window.addEventListener('keydown', onKey)
    return () => {
      canvas.removeEventListener('mousedown', onDown, true)
      canvas.removeEventListener('click', onClick, true)
      canvas.removeEventListener('mousemove', onMove)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mouseup', onUp)
      map.dragPan.enable()
      setStroke([])
    }
  }, [ref, enabled])

  const features: Feature[] = []
  if (stroke.length > 1) features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: stroke } })
  if (draft) features.push({ type: 'Feature', properties: {}, geometry: draft.polygon })
  const data: FeatureCollection = { type: 'FeatureCollection', features }
  return (
    <Source id="draw-preview" type="geojson" data={data}>
      <Layer id="draw-preview-fill" type="fill" filter={['==', ['geometry-type'], 'Polygon']} paint={{ 'fill-color': '#f59e0b', 'fill-opacity': 0.2 }} />
      <Layer id="draw-preview-line" type="line" paint={{ 'line-color': '#d97706', 'line-width': 2.5, 'line-dasharray': [2, 1] }} />
    </Source>
  )
}

// ---------- form ----------

export function DrawForm({
  draft,
  onSave,
  onCancel,
  onDelete,
}: {
  draft: Draft
  onSave: (d: Draft) => void
  onCancel: () => void
  onDelete?: () => void
}) {
  const [confidence, setConfidence] = useState(draft.confidence)
  const [text, setText] = useState(draft.text)
  const [types, setTypes] = useState<FaultType[]>(draft.faultTypes)
  const toggle = (t: FaultType) => setTypes((ts) => (ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t]))
  const set = (v: string) => setConfidence(Math.max(0, Math.min(1, Number(v) || 0)))
  return (
    <div className="absolute right-4 top-4 z-10 w-72 space-y-3 rounded-xl bg-white p-4 text-sm shadow-lg ring-1 ring-slate-200">
      <div className="font-semibold text-slate-900">{draft.id ? `Edit ${draft.id}` : 'New drawn area'}</div>
      <div className="space-y-1">
        <span className={`text-xs ${types.length ? 'text-slate-500' : 'font-medium text-red-600'}`}>
          Fault types{types.length ? '' : ' · select at least one'}
        </span>
        <div className="flex flex-wrap gap-1">
          {FAULT_TYPES.map((t) => {
            const on = types.includes(t)
            return (
              <button
                key={t}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(t)}
                className={`rounded px-1.5 py-px text-[11px] ring-1 transition ${
                  on ? 'bg-indigo-50 text-indigo-700 ring-indigo-300' : 'bg-white text-slate-400 ring-slate-200 hover:text-slate-600'
                }`}
              >
                {FAULT_STYLE[t].label}
              </button>
            )
          })}
        </div>
      </div>
      <label className="block space-y-1">
        <span className="flex justify-between text-xs text-slate-500">
          <span>Confidence weight (0–1)</span>
          <span className="tabular-nums text-slate-700">{confidence.toFixed(2)}</span>
        </span>
        <div className="flex items-center gap-2">
          <input type="range" min={0} max={1} step={0.05} value={confidence} onChange={(e) => set(e.target.value)} className="min-w-0 flex-1" />
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={confidence}
            onChange={(e) => set(e.target.value)}
            className="w-16 rounded-md border border-slate-200 px-1.5 py-1 text-xs"
          />
        </div>
      </label>
      <label className="block space-y-1">
        <span className="text-xs text-slate-500">Description (optional)</span>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          className="w-full resize-none rounded-md border border-slate-200 px-2 py-1 text-xs"
          placeholder="e.g. storm damage seen from the road"
        />
      </label>
      <div className="flex gap-2">
        <button
          onClick={() => onSave({ ...draft, faultTypes: FAULT_TYPES.filter((t) => types.includes(t)), confidence, text })}
          disabled={!types.length}
          className="flex-1 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white transition hover:bg-highlight hover:text-brand disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-brand disabled:hover:text-white"
        >
          Save
        </button>
        <button onClick={onCancel} className="flex-1 rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-700 transition hover:bg-slate-50">
          Cancel
        </button>
      </div>
      {onDelete && (
        <button onClick={onDelete} className="w-full text-xs text-rose-600 hover:underline">
          Delete area
        </button>
      )}
    </div>
  )
}

/** Sidebar list of drawn areas with edit / delete. */
export function DrawnList({
  reports,
  editingId,
  onEdit,
  onDelete,
}: {
  reports: Report[]
  editingId?: string
  onEdit: (r: Report) => void
  onDelete: (r: Report) => void
}) {
  if (!reports.length) return null
  return (
    <div>
      <div className="mb-1 text-xs text-slate-500">Drawn areas</div>
      <ul className="space-y-1">
        {reports.map((r) => (
          <li
            key={r.report_id}
            className={`flex items-center gap-2 rounded-md px-2 py-1 text-xs ${editingId === r.report_id ? 'bg-amber-50 ring-1 ring-amber-200' : 'bg-slate-50'}`}
          >
            <button onClick={() => onEdit(r)} className="min-w-0 flex-1 truncate text-left text-slate-700 hover:text-slate-900" title="Edit">
              <span className="font-medium">{r.report_id}</span> · {r.confidence.toFixed(2)}
              {r.text && <span className="text-slate-400"> · {r.text}</span>}
            </button>
            <button
              onClick={() => onDelete(r)}
              className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-slate-400 hover:bg-slate-200 hover:text-slate-700"
              title="Delete"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
