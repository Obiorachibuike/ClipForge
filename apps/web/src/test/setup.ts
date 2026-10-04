import '@testing-library/jest-dom/vitest';

// jsdom does not implement these; components that use them are common enough
// that stubbing them here keeps every test file free of boilerplate.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

if (!window.HTMLMediaElement.prototype.play) {
  window.HTMLMediaElement.prototype.play = () => Promise.resolve();
}
window.HTMLMediaElement.prototype.pause = () => undefined;
