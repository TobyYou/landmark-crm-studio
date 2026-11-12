// 小程序壳：通过 web-view 承载 H5，业务逻辑全部在 H5 / 后端，一处更新三端生效
App({
  // 部署后改成你的 HTTPS 域名（需在小程序后台“业务域名”中配置）
  globalData: { h5Url: 'https://leads.example.com/' },
});
