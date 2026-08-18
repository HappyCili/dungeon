# 活动签到：结论证据

1. `decrypted-js/main.js` 的消息枚举定义 `Activity_do_signin=21000`、`Activity_signin_sync=21002`、`Activity_signin_sync_all=21003`。
2. 同文件的 `ActivityModule.onGameData` 直接保存 `t.signinData`；`onDoSignin` 在 `ret == 0` 时以响应内 `act` 更新活动状态。
3. `Game_data` 编码器/解码器把 `signinData` 放在字段 `29`；签到活动模型将 `id`、`signinData`、`ticket` 分别放在字段 `1`、`3`、`5`。签到状态模型的 `todaySigned` 是字段 `3`，票券模型的 `status` 是字段 `2`。
4. 原生 `checkShowDailySignin` 的可领取条件包括 `ticket.status == 1`、`!todaySigned`、未满档，并在满足条件时发送 `21000 { id }`。
5. `signinPanel1New.js`、`signinPanel1.js` 和 `itemSigninBox.js` 的点击领取均仅发送 `{ id: signinId }` 到 `21000`。
6. 当前 UI 抓包记录显示两次 C2S `21003`（例如会话 `1785381544834672000`、序列 `85`），之后没有相同会话的 S2C `21003`；登录 `10490 Game_data` 已在该会话序列 `4` 到达。由此确认把 `21003` 当作请求-响应同步会造成等待超时。
