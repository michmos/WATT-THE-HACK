// Simulation setup (/setup): edit the hidden ground truth (dead lines, faults) and sensor/drone params.
// Edits stay in a local draft until "Save" (PUT /api/sim/config, which resets the sim).
import { useEffect, useState, type ChangeEvent, type ReactNode } from 'react'
import { Marker, Popup, type MapLayerMouseEvent } from 'react-map-gl/maplibre'
import type { Feature, Position } from 'geojson'
import {
  getPreset,
  getPresets,
  getSimConfig,
  putSimConfig,
  type FaultType,
  type LonLat,
  type SimConfig,
} from '../../api'
import { DEAD_COLOR, HIDDEN_DEAD_COLOR, Map, NetworkLayer } from '../../components/Map'
import { Section, SidebarLayout } from '../../components/Sidebar'
import { useStore } from '../../store'

export const FAULT_STYLE: Record<FaultType, { color: string; label: string; short: string }> = {
  vegetation: { color: '#16a34a', label: 'Vegetation', short: 'V' },
  sag: { color: '#2563eb', label: 'Sag', short: 'S' },
  ice: { color: '#0891b2', label: 'Ice', short: 'I' },
  galloping: { color: '#7c3aed', label: 'Galloping', short: 'G' },
  hotspot: { color: '#ea580c', label: 'Hotspot', short: 'H' },
  broken_conductor: { color: '#dc2626', label: 'Broken conductor', short: 'B' },
}
const FAULT_TYPES = Object.keys(FAULT_STYLE) as FaultType[]

const DRONE_LABELS: Record<keyof SimConfig['drone'], string> = {
  speed_inspect_mps: 'Inspect speed (m/s)',
  speed_transit_mps: 'Transit speed (m/s)',
  max_flight_min: 'Max flight (min)',
  full_charge_min: 'Full charge (min)',
  landing_min: 'Landing (min)',
  start_battery_min: 'Start battery (min)',
}

const btnGhost =
  'flex-1 cursor-pointer rounded-lg border border-slate-200 px-3 py-1.5 text-center text-sm text-slate-700 transition hover:bg-slate-50'
const numInput =
  'w-full rounded border border-slate-200 px-1 py-0.5 text-right text-xs tabular-nums [appearance:textfield] focus:border-slate-400 focus:outline-none [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none'

/** Nearest point on a (Multi)LineString, planar in lon/lat (fine at this scale). */
function snapToLine(f: Feature | undefined, p: LonLat): LonLat {
  const g = f?.geometry
  const lines: Position[][] = g?.type === 'LineString' ? [g.coordinates] : g?.type === 'MultiLineString' ? g.coordinates : []
  let best: LonLat = p
  let bestD = Infinity
  for (const line of lines)
    for (let i = 0; i + 1 < line.length; i++) {
      const [ax, ay] = line[i]
      const [bx, by] = line[i + 1]
      const dx = bx - ax
      const dy = by - ay
      const len2 = dx * dx + dy * dy
      const t = len2 ? Math.max(0, Math.min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / len2)) : 0
      const q: LonLat = [ax + t * dx, ay + t * dy]
      const d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
      if (d < bestD) [best, bestD] = [q, d]
    }
  return [+best[0].toFixed(6), +best[1].toFixed(6)]
}

function nextFaultId(faults: SimConfig['faults']) {
  const n = Math.max(0, ...faults.map((f) => Number(f.fault_id.replace(/\D/g, '')) || 0))
  return `F${n + 1}`
}

export function SimSetup() {
  const { staticData, state } = useStore()
  const [saved, setSaved] = useState<SimConfig | null>(null)
  const [draft, setDraft] = useState<SimConfig | null>(null)
  const [presets, setPresets] = useState<string[]>([])
  const [popover, setPopover] = useState<{ segmentId: string; position: LonLat } | null>(null)
  const [faultType, setFaultType] = useState<FaultType>('vegetation')
  const [hover, setHover] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    getSimConfig()
      .then((c) => {
        setSaved(c)
        setDraft(structuredClone(c))
      })
      .catch(() => setMsg('Could not load sim config'))
    getPresets().then(setPresets).catch(() => setPresets([]))
  }, [])

  const knownDead = state?.inputs.known_dead_segments ?? []
  const dirty = !!draft && JSON.stringify(draft) !== JSON.stringify(saved)
  const update = (fn: (c: SimConfig) => void) =>
    setDraft((d) => {
      if (!d) return d
      const c = structuredClone(d)
      fn(c)
      return c
    })

  const toggleHiddenDead = (id: string) =>
    update((c) => {
      c.dead_segments = c.dead_segments.includes(id) ? c.dead_segments.filter((s) => s !== id) : [...c.dead_segments, id]
    })

  const addFault = (segmentId: string, position: LonLat) => {
    update((c) => {
      c.faults.push({ fault_id: nextFaultId(c.faults), segment_id: segmentId, position, fault_type: faultType })
    })
    setPopover(null)
  }

  const onClick = (e: MapLayerMouseEvent) => {
    const id = e.features?.[0]?.properties?.segment_id as string | undefined
    if (!id) return setPopover(null)
    const seg = staticData?.segments.features.find((f) => f.properties?.segment_id === id)
    setPopover({ segmentId: id, position: snapToLine(seg, [e.lngLat.lng, e.lngLat.lat]) })
  }

  const save = async () => {
    if (!draft) return
    try {
      await putSimConfig(draft)
      setSaved(structuredClone(draft))
      setMsg('Saved · simulation reset')
    } catch (err) {
      setMsg(`Save failed: ${(err as Error).message}`)
    }
  }

  const loadPreset = async (name: string) => {
    if (!name) return
    try {
      setDraft(await getPreset(name))
      setMsg(`Loaded preset "${name}" · not saved yet`)
    } catch (err) {
      setMsg(`Could not load preset: ${(err as Error).message}`)
    }
  }

  const download = () => {
    if (!draft) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' }))
    const a = Object.assign(document.createElement('a'), { href: url, download: 'scenario.json' })
    a.click()
    URL.revokeObjectURL(url)
  }

  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || !saved) return
    try {
      const c = JSON.parse(await file.text()) as Partial<SimConfig>
      setDraft({ ...structuredClone(saved), ...c })
      setMsg(`Loaded ${file.name} · not saved yet`)
    } catch (err) {
      setMsg(`Could not read ${file.name}: ${(err as Error).message}`)
    }
  }

  const popSeg = popover?.segmentId
  const popKnownDead = !!popSeg && knownDead.includes(popSeg)
  const popHiddenDead = !!popSeg && !!draft?.dead_segments.includes(popSeg)

  return (
    <SidebarLayout
      sidebar={
        <>
          <Section title="Scenario">
            <select
              value=""
              onChange={(e) => loadPreset(e.target.value)}
              className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">Load preset…</option>
              {presets.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <div className="mt-2 flex gap-2">
              <button className={btnGhost} onClick={download} disabled={!draft}>
                Download JSON
              </button>
              <label className={btnGhost}>
                Upload JSON
                <input type="file" accept=".json,application/json" className="hidden" onChange={upload} />
              </label>
            </div>
          </Section>

          <Section title="Dead lines">
            <p className="mb-1.5 text-xs text-slate-500">Click a line on the map to change it.</p>
            <div className="flex flex-wrap gap-1.5">
              {draft?.dead_segments.map((s) => (
                <Chip key={s} color={HIDDEN_DEAD_COLOR} onRemove={() => toggleHiddenDead(s)}>
                  {s}
                </Chip>
              ))}
              {knownDead.map((s) => (
                <Chip key={s} color={DEAD_COLOR} title="Marked by the operator (read-only here)">
                  {s} · operator
                </Chip>
              ))}
              {!draft?.dead_segments.length && !knownDead.length && <span className="text-xs text-slate-400">none</span>}
            </div>
          </Section>

          <Section title={`Faults (${draft?.faults.length ?? 0})`}>
            <ul className="space-y-0.5">
              {draft?.faults.map((f, i) => (
                <li key={f.fault_id + i} className="group flex items-center gap-2 rounded-md px-1 py-0.5 text-sm hover:bg-slate-50">
                  <FaultDot type={f.fault_type} />
                  <span className="text-slate-800">{FAULT_STYLE[f.fault_type].label}</span>
                  <span className="text-xs text-slate-400">{f.segment_id}</span>
                  <button
                    onClick={() => update((c) => void c.faults.splice(i, 1))}
                    className="ml-auto flex h-5 w-5 items-center justify-center rounded-full text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                    title="Remove"
                  >
                    ×
                  </button>
                </li>
              ))}
              {!draft?.faults.length && <li className="text-xs text-slate-400">none · click a line to add one</li>}
            </ul>
          </Section>

          {draft && (
            <Section title="Parameters">
              <Collapsible title="Fault detection probabilities">
                <table className="w-full text-xs">
                  <thead className="text-slate-400">
                    <tr>
                      <th className="text-left font-normal" />
                      <th className="px-0.5 font-normal">air</th>
                      <th className="px-0.5 font-normal">landed</th>
                      <th className="px-0.5 font-normal">false +</th>
                    </tr>
                  </thead>
                  <tbody>
                    {FAULT_TYPES.filter((t) => draft.sensors[t]).map((t) => (
                      <tr key={t} title={draft.sensors[t]?.sensor}>
                        <td className="py-0.5 pr-1 text-slate-600">{FAULT_STYLE[t].label}</td>
                        {(['p_detect_air', 'p_detect_landed', 'p_false_positive'] as const).map((k) => (
                          <td key={k} className="w-[3.25rem] px-0.5">
                            <input
                              type="number"
                              min={0}
                              max={1}
                              step={0.001}
                              className={numInput}
                              value={draft.sensors[t]![k]}
                              onChange={(e) => update((c) => void (c.sensors[t]![k] = Number(e.target.value)))}
                            />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Collapsible>
              <Collapsible title="Drone">
                <div className="space-y-1">
                  {(Object.keys(DRONE_LABELS) as (keyof SimConfig['drone'])[]).map((k) => (
                    <label key={k} className="flex items-center gap-2 text-xs text-slate-600">
                      <span className="flex-1">{DRONE_LABELS[k]}</span>
                      <input
                        type="number"
                        min={0}
                        className={numInput + ' w-16'}
                        value={draft.drone[k]}
                        onChange={(e) => update((c) => void (c.drone[k] = Number(e.target.value)))}
                      />
                    </label>
                  ))}
                  <label className="flex items-center gap-2 text-xs text-slate-600">
                    <span className="flex-1">Random seed</span>
                    <input
                      type="number"
                      className={numInput + ' w-16'}
                      value={draft.seed}
                      onChange={(e) => update((c) => void (c.seed = Math.round(Number(e.target.value))))}
                    />
                  </label>
                </div>
              </Collapsible>
            </Section>
          )}

          <div className="sticky bottom-0 -mx-4 mt-auto -mb-4 border-t border-slate-200 bg-white p-4">
            <div className="flex gap-2">
              <button
                onClick={save}
                disabled={!dirty}
                className="flex-1 rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white transition enabled:hover:bg-highlight enabled:hover:text-brand disabled:opacity-40"
              >
                Save scenario
              </button>
              {dirty && (
                <button className={btnGhost + ' flex-none'} onClick={() => saved && setDraft(structuredClone(saved))}>
                  Discard
                </button>
              )}
            </div>
            <p className="mt-1.5 text-xs text-slate-400">{msg ?? (dirty ? 'Unsaved changes · saving resets the simulation' : 'Saving resets the simulation')}</p>
          </div>
        </>
      }
    >
      <Map
        interactiveLayerIds={['segments-hit']}
        onClick={onClick}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        cursor={hover ? 'pointer' : 'grab'}
      >
        <NetworkLayer dead={knownDead} hiddenDead={draft?.dead_segments ?? []} />
        {draft?.faults.map((f, i) => (
          <Marker key={f.fault_id + i} longitude={f.position[0]} latitude={f.position[1]}>
            <div title={`${f.fault_id} · ${FAULT_STYLE[f.fault_type].label} · ${f.segment_id}`}>
              <FaultDot type={f.fault_type} big />
            </div>
          </Marker>
        ))}
        {popover && popSeg && (
          <Popup
            longitude={popover.position[0]}
            latitude={popover.position[1]}
            closeOnClick={false}
            onClose={() => setPopover(null)}
            offset={10}
            maxWidth="240px"
          >
            <div className="w-52 space-y-2 p-0.5 text-sm">
              <div className="font-semibold">{popSeg}</div>
              {popKnownDead ? (
                <p className="text-xs text-slate-500">Marked dead by the operator (read-only here).</p>
              ) : (
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={popHiddenDead}
                    onChange={() => toggleHiddenDead(popSeg)}
                    style={{ accentColor: HIDDEN_DEAD_COLOR }}
                  />
                  Mark dead (hidden)
                </label>
              )}
              <div className="flex gap-1.5 border-t border-slate-100 pt-2">
                <select
                  value={faultType}
                  onChange={(e) => setFaultType(e.target.value as FaultType)}
                  className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-1 py-1 text-xs"
                >
                  {FAULT_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {FAULT_STYLE[t].label}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => addFault(popSeg, popover.position)}
                  className="rounded-md bg-brand px-2 py-1 text-xs font-medium text-white transition hover:bg-highlight hover:text-brand"
                >
                  Add fault here
                </button>
              </div>
            </div>
          </Popup>
        )}
      </Map>
      <SetupLegend />
    </SidebarLayout>
  )
}

function FaultDot({ type, big }: { type: FaultType; big?: boolean }) {
  const s = FAULT_STYLE[type]
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-full font-semibold text-white ring-2 ring-white ${
        big ? 'h-6 w-6 text-[11px] shadow-md' : 'h-4 w-4 text-[9px]'
      }`}
      style={{ background: s.color }}
    >
      {s.short}
    </span>
  )
}

function Chip({ color, children, onRemove, title }: { color: string; children: ReactNode; onRemove?: () => void; title?: string }) {
  return (
    <span title={title} className="flex items-center gap-1 rounded-full bg-slate-100 py-0.5 pl-2 pr-1 text-xs text-slate-700">
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {children}
      {onRemove ? (
        <button
          onClick={onRemove}
          className="flex h-4 w-4 items-center justify-center rounded-full text-slate-400 hover:bg-slate-200 hover:text-slate-700"
          title="Remove"
        >
          ×
        </button>
      ) : (
        <span className="w-1" />
      )}
    </span>
  )
}

function Collapsible({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="group mb-2 rounded-lg border border-slate-200">
      <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-sm text-slate-700">
        {title}
        <span className="text-slate-400 transition group-open:rotate-90">›</span>
      </summary>
      <div className="border-t border-slate-100 px-3 py-2">{children}</div>
    </details>
  )
}

function SetupLegend() {
  const line = (color: string) => (
    <span className="block w-6" style={{ borderTopWidth: 4, borderTopStyle: 'dashed', borderColor: color }} />
  )
  return (
    <div className="absolute bottom-3 left-3 space-y-1.5 rounded-xl bg-white/95 p-3 text-xs text-slate-600 shadow-sm ring-1 ring-slate-200">
      <div className="flex items-center gap-2">
        {line(HIDDEN_DEAD_COLOR)} Dead (hidden ground truth)
      </div>
      <div className="flex items-center gap-2">
        {line(DEAD_COLOR)} Dead (known to operator)
      </div>
      <div className="flex flex-wrap gap-x-2 gap-y-1 pt-0.5">
        {FAULT_TYPES.map((t) => (
          <span key={t} className="flex items-center gap-1">
            <FaultDot type={t} /> {FAULT_STYLE[t].label}
          </span>
        ))}
      </div>
    </div>
  )
}
