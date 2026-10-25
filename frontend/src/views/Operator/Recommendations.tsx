// Recommendations: derived from `recommendation` events, shown pinned in the sidebar
// and as markers on the map. One entry per place (cell / segment / report area); the latest wins,
// so "Needs confirmation" is replaced by "Send repair crew" once the evidence is strong enough.
import { Marker } from 'react-map-gl/maplibre'
import type { LonLat, SimEvent, State } from '../../api'

export type RecKind = 'crew' | 'confirm' | 'ground' | 'cleared'

export interface Rec {
  key: string
  kind: RecKind
  event: SimEvent
  position: LonLat | null
  /** confirm whose cell risk has since dropped below the threshold (checked again, nothing there) */
  resolved: boolean
}

export const REC_STYLE: Record<RecKind, { label: string; icon: string; color: string; card: string }> = {
  crew: { label: 'Send crew', icon: '!', color: '#dc2626', card: 'bg-rose-50 ring-rose-200 text-rose-950' },
  confirm: { label: 'Confirm', icon: '?', color: '#f59e0b', card: 'bg-amber-50 ring-amber-200 text-amber-950' },
  ground: { label: 'Ground check', icon: '⚡', color: '#475569', card: 'bg-slate-50 ring-slate-200 text-slate-800' },
  cleared: { label: 'Cleared', icon: '✓', color: '#16a34a', card: 'bg-emerald-50 ring-emerald-200 text-emerald-950' },
}
const ORDER: RecKind[] = ['crew', 'confirm', 'ground', 'cleared']
const CONFIRM_RISK = 0.4 // backend threshold (planner/recommend.py)

function kindOf(msg: string): RecKind {
  if (msg.startsWith('Send repair crew')) return 'crew'
  if (msg.startsWith('Needs confirmation')) return 'confirm'
  if (msg.startsWith('Area of')) return 'cleared'
  return 'ground'
}

/** Centre of a report polygon's bounding box (area-cleared events carry no position). */
function reportCenter(state: State, msg: string): LonLat | null {
  const id = /^Area of (\S+) cleared/.exec(msg)?.[1]
  const ring = state.inputs.reports.find((r) => r.report_id === id)?.polygon.coordinates[0]
  if (!ring?.length) return null
  const lons = ring.map((c) => c[0])
  const lats = ring.map((c) => c[1])
  return [(Math.min(...lons) + Math.max(...lons)) / 2, (Math.min(...lats) + Math.max(...lats)) / 2]
}

export function recommendations(state: State | null): Rec[] {
  if (!state) return []
  const risk = new globalThis.Map(state.cells.map((c) => [c.cell_id, c.risk]))
  const byKey = new globalThis.Map<string, Rec>()
  for (const e of state.events) {
    if (e.type !== 'recommendation') continue
    const kind = kindOf(e.message)
    const key = e.cell_id ?? e.segment_id ?? e.message
    const position = (e.position as LonLat | null | undefined) ?? (kind === 'cleared' ? reportCenter(state, e.message) : null)
    const resolved = kind === 'confirm' && (risk.get(e.cell_id ?? '') ?? 1) < CONFIRM_RISK
    byKey.delete(key) // re-insert so the latest is last
    byKey.set(key, { key, kind, event: e, position, resolved })
  }
  return [...byKey.values()].reverse().sort((a, b) => +a.resolved - +b.resolved || ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind))
}

/** Small pins on the map; crew/confirm/ground stand out, area-cleared is a subtle tick. */
export function RecommendationMarkers({ recs, onSelect }: { recs: Rec[]; onSelect: (r: Rec) => void }) {
  return (
    <>
      {recs
        .filter((r) => r.position && !r.resolved)
        .map((r) => {
          const s = REC_STYLE[r.kind]
          const subtle = r.kind === 'cleared'
          return (
            <Marker key={r.key} longitude={r.position![0]} latitude={r.position![1]} anchor={subtle ? 'center' : 'bottom'}>
              <button
                onClick={() => onSelect(r)}
                title={r.event.message}
                className={
                  subtle
                    ? 'flex h-5 w-5 items-center justify-center rounded-full bg-white/90 text-[11px] font-bold shadow-sm ring-1 ring-emerald-300'
                    : 'flex flex-col items-center drop-shadow-md'
                }
                style={subtle ? { color: s.color } : undefined}
              >
                {subtle ? (
                  s.icon
                ) : (
                  <>
                    <span
                      className="flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold text-white ring-2 ring-white"
                      style={{ background: s.color }}
                    >
                      {s.icon}
                    </span>
                    <span className="-mt-0.5 h-2 w-0.5" style={{ background: s.color }} />
                  </>
                )}
              </button>
            </Marker>
          )
        })}
    </>
  )
}

/** Pinned list in the sidebar: what the operator should act on. */
export function RecommendationList({ recs, selected, onSelect }: { recs: Rec[]; selected?: string | null; onSelect: (r: Rec) => void }) {
  if (!recs.length)
    return <p className="px-1 text-sm text-slate-400">None yet. They appear once the evidence for a place is strong enough.</p>
  return (
    <ul className="space-y-1.5">
      {recs.map((r) => {
        const s = REC_STYLE[r.kind]
        return (
          <li key={r.key}>
            <button
              onClick={() => onSelect(r)}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] leading-snug ring-1 transition hover:brightness-95 ${
                r.resolved ? 'bg-white text-slate-400 ring-slate-100 line-through' : s.card
              } ${r.key === selected ? 'ring-2 !ring-slate-500' : ''}`}
            >
              <span
                className="mt-px flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-white"
                style={{ background: r.resolved ? '#cbd5e1' : s.color }}
              >
                {s.icon}
              </span>
              <span className="min-w-0">{r.event.message}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
