import '@testing-library/jest-dom/vitest';

/**
 * `window.matchMedia` 的替身。
 *
 * **jsdom 不实现它**，而 antd 的响应式栅格在挂载时就订阅它
 * （`_util/responsiveObserver.js:99`）—— 于是任何渲染了 antd 组件的用例
 * 都以 `TypeError: window.matchMedia is not a function` 失败，
 * **而那个报错与被测代码毫无关系**。
 *
 * **永远回 `matches: false`，并且不假装会变。** 一个会「响应」的替身要
 * 决定在测试里什么算窄屏、什么算宽屏，而没有任何用例问过这个问题；
 * 编一个答案出来，下一个人会以为断点是被测过的。
 * 这里只让订阅能建立，**它不是一个响应式的模型**。
 */
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => undefined,
        removeListener: () => undefined,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  });
}
