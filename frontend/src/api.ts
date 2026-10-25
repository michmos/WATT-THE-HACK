// API types + fetch helpers + polling hook.
// Types are generated from the backend contract (`npm run gen:types` -> src/api-types.ts);
// below we alias them and tighten the loosely-typed fields (GeoJSON, [lon,lat] tuples).

import { useEffect, useState } from 'react'
import type { Feature, FeatureCollection, LineString, Point, Polygon } from 'geojson'
import type { components } from './api-types'

// ---------- types (generated) ----------

type S = components['schemas']
type With<T, U> = Omit<T, keyof U> & U

export type LonLat = [number, number]
export type FaultType = S['Fault']['fault_type']

export type Report = With<S['Report'], { polygon: Polygon }>
export type OperatorInputs = With<S['OperatorInputs'], { reports: Report[] }>
export type StartPosition = With<S['StartPosition'], { position: LonLat }>
export type EventType = S['Event']['type']
export type SimEvent = With<S['Event'], { position?: LonLat | null }>
export type DroneState = With<S['DroneState'], { position: LonLat; route: LonLat[] }>
export type CellState = S['CellState']
export type SegmentState = S['SegmentState']
export type CellObservation = S['CellObservation']
export type SimStatus = S['State']['status']
export type Fault = With<S['Fault'], { position: LonLat }>
export type SensorParams = S['SensorParams']
export type SimControl = S['SimControl']
export type SimConfig = With<S['SimConfig'], { faults: Fault[] }>

export interface Static {
  segments: FeatureCollection<LineString>
  cells: FeatureCollection<LineString>
  nodes: FeatureCollection<Point>
  storm: Feature<Polygon>
  default_inputs: OperatorInputs
  vegetation?: FeatureCollection | null
}

/** t is in sim seconds. drones is empty in setup until POST /plan/start-positions; route is empty unless flying.
 * Speed change = postSimControl({action: 'resume', speed}) (also resumes if paused). */
export type State = With<
  S['State'],
  { drones: DroneState[]; inputs: OperatorInputs; start_positions: StartPosition[]; events: SimEvent[] }
>

// ---------- mock mode (VITE_MOCK=1) ----------

export const MOCK = import.meta.env.VITE_MOCK === '1'

// glob so the build still works while the mock files don't exist yet
// only bundled in mock mode (VITE_MOCK is a build-time constant, so the JSON is tree-shaken otherwise)
const mockFiles: Record<string, { default: unknown }> = MOCK
  ? import.meta.glob<{ default: unknown }>('./mocks/*.json', { eager: true })
  : {}
const mockFile = <T>(name: string, fallback: T): T =>
  structuredClone((mockFiles[`./mocks/${name}.json`]?.default as T) ?? fallback)

const emptyFc = { type: 'FeatureCollection', features: [] } as FeatureCollection<never>
const emptyInputs: OperatorInputs = { reports: [], known_dead_segments: [] }
const mockStatic: Static = mockFile('static', {
  segments: emptyFc,
  cells: emptyFc,
  nodes: emptyFc,
  storm: { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [] } },
  default_inputs: emptyInputs,
})
const mockState: State = mockFile('state', {
  t: 0,
  speed: 60,
  status: 'setup',
  drones: [],
  cells: [],
  segments: [],
  inputs: emptyInputs,
  start_positions: [],
  events: [],
  progress: { total: 0, found: [] },
  observations: [],
})
let mockConfig: SimConfig | null = mockFile<SimConfig | null>('sim_config', null)
let mockLastTick = performance.now()

function mockTick(): State {
  const now = performance.now()
  if (mockState.status === 'running') mockState.t += ((now - mockLastTick) / 1000) * mockState.speed
  mockLastTick = now
  return structuredClone(mockState)
}

function mockControl(c: SimControl): State {
  mockTick()
  if (c.speed) mockState.speed = c.speed
  if (c.action === 'start' || c.action === 'resume') mockState.status = 'running'
  if (c.action === 'pause') mockState.status = 'paused'
  if (c.action === 'reset') Object.assign(mockState, { status: 'setup', t: 0 })
  return structuredClone(mockState)
}

// ---------- fetch helpers ----------

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status}`)
  return res.json() as Promise<T>
}

const mock = <T>(value: T) => Promise.resolve(value)

export const getStatic = () => (MOCK ? mock(structuredClone(mockStatic)) : req<Static>('GET', '/static'))

export const getState = () => (MOCK ? mock(mockTick()) : req<State>('GET', '/state'))

export const putInputs = (inputs: OperatorInputs) =>
  MOCK ? mock((mockState.inputs = inputs)) : req<unknown>('PUT', '/inputs', inputs)

export const postStartPositions = () =>
  MOCK ? mock(mockState.start_positions) : req<StartPosition[]>('POST', '/plan/start-positions')

export const getSimConfig = () =>
  MOCK ? mock(mockConfig as SimConfig) : req<SimConfig>('GET', '/sim/config')

export const putSimConfig = (config: SimConfig) =>
  MOCK ? mock((mockConfig = config)) : req<unknown>('PUT', '/sim/config', config)

export const getPresets = () => (MOCK ? mock(['default']) : req<string[]>('GET', '/sim/presets'))

export const getPreset = (name: string) =>
  MOCK ? mock(structuredClone(mockConfig) as SimConfig) : req<SimConfig>('GET', `/sim/presets/${encodeURIComponent(name)}`)

export const postSimControl = (control: SimControl) =>
  MOCK ? mock(mockControl(control)) : req<unknown>('POST', '/sim/control', control)

// ---------- hooks ----------

/** Polls GET /api/state every `intervalMs`; returns the latest snapshot (null until the first one). */
export function usePolling(intervalMs = 500): State | null {
  const [state, setState] = useState<State | null>(null)
  useEffect(() => {
    let alive = true
    let timer: number
    const tick = async () => {
      try {
        const s = await getState()
        if (alive) setState(s)
      } catch {
        // backend not up yet; keep last snapshot and retry
      }
      if (alive) timer = window.setTimeout(tick, intervalMs)
    }
    tick()
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [intervalMs])
  return state
}

/** Fetches GET /api/static once. */
export function useStatic(): Static | null {
  const [data, setData] = useState<Static | null>(null)
  useEffect(() => {
    getStatic().then(setData).catch(() => setData(null))
  }, [])
  return data
}
