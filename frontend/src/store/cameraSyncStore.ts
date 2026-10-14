import { create } from 'zustand'

interface Vec { x: number; y: number; z: number }

/**
 * Keeps the two side-by-side compare viewers looking at the SAME physical spot:
 * the driver publishes its full pose (camera position, orbit target, up) in A's
 * scene frame, and the follower maps it into its own frame through the alignment
 * matrix. So zoom onto a place in A and B shows exactly that place. (A tiny
 * detail against a whole building will look small when you're zoomed out — that
 * is correct; zoom in on the shared area to compare it.)
 */
interface CameraSyncState {
  matrixBA: number[] | null   // B_scene → A_scene, row-major 4×4 (from /compare)
  driver: 'a' | 'b' | null
  pos: Vec | null             // camera position, in A's frame
  target: Vec | null          // orbit target, in A's frame
  up: Vec | null              // camera up, in A's frame
  version: number

  setMatrixBA: (m: number[] | null) => void
  setDriver: (d: 'a' | 'b' | null) => void
  publish: (pos: Vec, target: Vec, up: Vec) => void
  reset: () => void
}

export const useCameraSyncStore = create<CameraSyncState>((set) => ({
  matrixBA: null,
  driver: null,
  pos: null,
  target: null,
  up: null,
  version: 0,
  setMatrixBA: (matrixBA) => set({ matrixBA }),
  setDriver: (driver) => set({ driver }),
  publish: (pos, target, up) => set((s) => ({ pos, target, up, version: s.version + 1 })),
  reset: () => set({ driver: null, pos: null, target: null, up: null, version: 0 }),
}))
