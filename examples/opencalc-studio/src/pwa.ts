export type ApplyPwaUpdate = () => Promise<void>;
type PwaUpdateListener = (applyUpdate: ApplyPwaUpdate) => void;

let pendingUpdate: ApplyPwaUpdate | null = null;
const updateListeners = new Set<PwaUpdateListener>();

export function announcePwaUpdate(applyUpdate: ApplyPwaUpdate) {
  pendingUpdate = applyUpdate;
  updateListeners.forEach((listener) => listener(applyUpdate));
}

export function subscribeToPwaUpdate(listener: PwaUpdateListener) {
  updateListeners.add(listener);
  if (pendingUpdate) {
    listener(pendingUpdate);
  }

  return () => {
    updateListeners.delete(listener);
  };
}

export function dismissPwaUpdate() {
  pendingUpdate = null;
}
