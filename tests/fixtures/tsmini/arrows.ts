export const fetchUser = async (id: string): Promise<string> => {
  return lookup(id);
};

const cache = new Map<string, string>();

const lookup = (id: string): string => cache.get(id) ?? "";

export class EventHub {
  private handlers = new Map<string, () => void>();

  onEvent = (name: string): void => {
    this.dispatch(name);
  };

  private dispatch(name: string): void {
    this.handlers.get(name)?.();
  }
}
