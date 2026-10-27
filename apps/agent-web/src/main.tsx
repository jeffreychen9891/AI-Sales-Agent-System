import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';

const container = document.getElementById('root');
// **抛，不是静默返回。** 一个找不到挂载点的应用应当在启动时炸，
// 而不是渲染出一个空白页 —— 后者与「后端没返回数据」在屏幕上一模一样。
if (container === null) throw new Error('#root is missing from index.html');

createRoot(container).render(
  // `ConfigProvider` 在 `App` 里，不在这里 —— 理由见 `App.tsx`。
  <StrictMode>
    <App />
  </StrictMode>,
);
