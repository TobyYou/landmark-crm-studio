'use strict';
/** 统一时钟：生产环境为真实时间；测试中可替换 now() 来模拟“30 天后” */
const clock = {
  offset: 0,
  now() { return Date.now() + clock.offset; },
};
const DAY = 24 * 3600 * 1000;
module.exports = clock;
module.exports.DAY = DAY;
