// One store for both views: static data (fetched once) + the latest /state snapshot.
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { usePolling, useStatic, type State, type Static } from './api'

interface Store {
  staticData: Static | null
  state: State | null
  inputWarning: boolean
  warnMissingInputs: () => void
}

const StoreContext = createContext<Store>({ staticData: null, state: null, inputWarning: false, warnMissingInputs: () => {} })

export function StoreProvider({ children }: { children: ReactNode }) {
  const staticData = useStatic()
  const state = usePolling(500)
  return <StoreContext.Provider value={{ staticData, state }}>{children}</StoreContext.Provider>
}

export const useStore = () => useContext(StoreContext)
