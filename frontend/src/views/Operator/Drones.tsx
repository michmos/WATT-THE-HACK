// Drones: HTML markers (heading arrow + battery ring) moved imperatively in a requestAnimationFrame
// loop, interpolating between /state snapshots, plus a fading flown-path trail
// kept client-side. No React re-render per frame.
import { useEffect, useRef } from 'react'
import { Marker as MlMarker, type GeoJSONSource } from 'maplibre-gl'
import { Layer, Source, useMap } from 'react-map-gl/maplibre'
import type { FeatureCollection, LineString } from 'geojson'
import type { DroneState, LonLat, State } from '../../api'

export const DRONE_COLORS: Record<DroneState['status'], string> = {
  flying: '#2563eb',
  landing: '#2563eb',
  charging: '#16a34a',
  stranded: '#e11d48',
}
const TRAIL_SIM_S = 15 * 60 // trail shows the last 15 sim minutes

interface Anim {
  marker: MlMarker
  el: HTMLDivElement
  from: LonLat
  to: LonLat
  t0: number
  dur: number
  trail: { t: number; p: LonLat }[]
  html: string
  atRest: boolean
}

function droneSvg(d: DroneState) {
  const c = DRONE_COLORS[d.status]
  const r = 15
  const circ = 2 * Math.PI * r
  const frac = Math.max(0, Math.min(1, d.battery_min / (d.max_flight_min || 30)))
  return `
  <svg width="38" height="38" viewBox="0 0 38 38">
    <circle cx="19" cy="19" r="${r}" fill="white" stroke="#e2e8f0" stroke-width="3.5"/>
    <circle cx="19" cy="19" r="${r}" fill="none" stroke="${c}" stroke-width="3.5" stroke-linecap="round"
      stroke-dasharray="${frac * circ} ${circ}" transform="rotate(-90 19 19)"/>
    <g transform="rotate(${d.heading} 19 19)">
      <path d="M19 9.5 L25 25 L19 21.5 L13 25 Z" fill="${d.status === 'charging' ? '#64748b' : '#0f172a'}"/>
    </g>
  </svg>
  <div style="position:absolute;top:36px;left:50%;transform:translateX(-50%);font:600 10px/1 Inter,system-ui,sans-serif;
    color:#0f172a;background:rgba(255,255,255,.85);padding:1px 4px;border-radius:4px;white-space:nowrap">
    ${d.drone_id} · ${Math.round(d.battery_min)}′</div>`
}

const lerp = (a: LonLat, b: LonLat, k: number): LonLat => [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k]

const emptyFc: FeatureCollection<LineString> = { type: 'FeatureCollection', features: [] }

export function Drones({ state }: { state: State | null }) {
  const { current: mapRef } = useMap()
  const anims = useRef(new Map<string, Anim>())
  const lastPoll = useRef(performance.now())
  const lastT = useRef(0)
  const dirty = useRef(true) // something to redraw (avoid map repaints when nothing moves)

  // new snapshot -> retarget the animations
  useEffect(() => {
    const map = mapRef?.getMap()
    if (!map || !state) return
    const now = performance.now()
    const dur = Math.min(1000, Math.max(200, now - lastPoll.current))
    lastPoll.current = now
    const reset = state.t < lastT.current
    lastT.current = state.t

    const visible = state.status === 'setup' ? [] : state.drones
    const seen = new Set<string>()
    for (const d of visible) {
      seen.add(d.drone_id)
      let a = anims.current.get(d.drone_id)
      if (!a) {
        const el = document.createElement('div')
        // no `position` here: MapLibre's .maplibregl-marker class makes it absolute (top:0;left:0) and positions it
        // via transform. An inline position:relative put the markers in normal flow, each pushed 38px below the previous.
        el.style.cssText = 'width:38px;height:38px;pointer-events:none'
        const marker = new MlMarker({ element: el }).setLngLat(d.position).addTo(map)
        a = { marker, el, from: d.position, to: d.position, t0: now, dur, trail: [], html: '', atRest: true }
        anims.current.set(d.drone_id, a)
        dirty.current = true
      }
      if (reset) a.trail = []
      if (a.to[0] !== d.position[0] || a.to[1] !== d.position[1] || reset) dirty.current = true
      const cur = a.marker.getLngLat()
      a.from = [cur.lng, cur.lat]
      a.to = d.position
      a.t0 = now
      a.dur = dur
      a.atRest = a.from[0] === a.to[0] && a.from[1] === a.to[1]
      const html = droneSvg(d)
      if (a.html !== html) a.el.innerHTML = a.html = html
      const last = a.trail[a.trail.length - 1]?.p
      if (!last || last[0] !== d.position[0] || last[1] !== d.position[1]) a.trail.push({ t: state.t, p: d.position })
      while (a.trail.length && a.trail[0].t < state.t - TRAIL_SIM_S) a.trail.shift()
    }
    for (const [id, a] of anims.current) {
      if (!seen.has(id)) {
        a.marker.remove()
        anims.current.delete(id)
        dirty.current = true
      }
    }
  }, [mapRef, state])

  // animation loop
  useEffect(() => {
    const map = mapRef?.getMap()
    if (!map) return
    let raf = 0
    let lastTrail = 0
    const frame = (now: number) => {
      const features: FeatureCollection<LineString>['features'] = []
      let moving = false
      for (const a of anims.current.values()) {
        const k = Math.min(1, (now - a.t0) / a.dur)
        const p = lerp(a.from, a.to, k)
        if (!a.atRest) {
          a.marker.setLngLat(p)
          moving = true
          a.atRest = k >= 1
        }
        const coords = [...a.trail.slice(0, -1).map((x) => x.p), p]
        if (coords.length > 1) features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } })
      }
      if ((moving || dirty.current) && now - lastTrail > 80) {
        lastTrail = now
        dirty.current = moving
        ;(map.getSource('trails') as GeoJSONSource | undefined)?.setData({ type: 'FeatureCollection', features })
      }
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [mapRef])

  // remove markers on unmount
  useEffect(() => {
    const all = anims.current
    return () => {
      for (const a of all.values()) a.marker.remove()
      all.clear()
    }
  }, [])

  return (
    <Source id="trails" type="geojson" data={emptyFc} lineMetrics>
      <Layer
        id="trails-line"
        type="line"
        layout={{ 'line-cap': 'round', 'line-join': 'round' }}
        paint={{
          'line-width': 2.5,
          'line-gradient': ['interpolate', ['linear'], ['line-progress'], 0, 'rgba(37,99,235,0)', 1, 'rgba(37,99,235,0.55)'],
        }}
      />
    </Source>
  )
}
