const app = getApp();
Page({
  data: { url: '' },
  onLoad() { this.setData({ url: app.globalData.h5Url }); },
  onShareAppMessage() { return { title: '地标传媒线索公海', path: '/pages/index/index' }; },
});
