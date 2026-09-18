/* Display-only labels. Semantic work titles and IDs are owned by the server. */
(function (global) {
  'use strict';
  function flowShortLabel(node) {
    if (!node || typeof node !== 'object') return '工作记录';
    const label = typeof node.label === 'string' ? node.label : '';
    if (node.kind === 'action') return label.split(/\s*[·｜|]\s*/)[0].trim() || '执行记录';
    return label || (node.kind === 'project' ? '项目' : node.kind === 'stage' ? '阶段' : '工作记录');
  }
  global.flowShortLabel = flowShortLabel;
})(typeof window !== 'undefined' ? window : globalThis);
