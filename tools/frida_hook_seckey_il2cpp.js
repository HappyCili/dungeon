/**
 * 依赖 frida-il2cpp-bridge，在 IL2CPP 层读取 BuildTimeData.secKey 并 hook Pack1Decode。
 *
 * 安装（主机）:
 *   pip install frida-tools
 *   # 将 frida-il2cpp-bridge 放到可 require 路径，或用下面的 RPC 版思路
 *
 * 设备:
 *   - 模拟器/真机已 root（或可调试包）
 *   - 运行与 frida-tools 主版本匹配的 frida-server
 *   - 已安装游戏: com.zygames.dungeon4
 *
 * 启动:
 *   frida -U -f com.zygames.dungeon4 -l tools/frida_hook_seckey_il2cpp.js --no-pause
 *
 * 成功时会打印类似:
 *   [secKey] BuildTimeData.secKey = "xxxxxxxx"
 *   [secKey] Pack1Decode password = "xxxxxxxx" path/table hint...
 */

'use strict';

// 若使用官方 bridge，取消注释并保证模块可加载:
// const Il2Cpp = require('frida-il2cpp-bridge');

function log(msg) {
  console.log('[secKey] ' + msg);
}

function readCsString(strObj) {
  if (strObj === null || strObj.isNull && strObj.isNull()) return null;
  try {
    return strObj.content !== undefined ? strObj.content : strObj.toString();
  } catch (e) {
    try {
      return strObj.toString();
    } catch (e2) {
      return '<unreadable>';
    }
  }
}

function dumpBuildTimeData(obj) {
  if (!obj || (obj.isNull && obj.isNull())) {
    log('BuildTimeData 为空');
    return;
  }
  var fields = ['secKey', 'SecKey', 'encryptPaths', 'resVer', 'version', 'updateInfoURL', 'downloadURL', 'splitMode', 'channel'];
  fields.forEach(function (name) {
    try {
      var f = obj.field(name);
      if (!f) return;
      var v = f.value;
      if (v && v.content !== undefined) {
        log('BuildTimeData.' + name + ' = ' + JSON.stringify(v.content));
      } else if (v && v.toString) {
        log('BuildTimeData.' + name + ' = ' + v.toString());
      } else {
        log('BuildTimeData.' + name + ' = ' + v);
      }
    } catch (e) {
      // 字段不存在则忽略
    }
  });

  // 枚举所有实例字段（不同版本字段名可能变化）
  try {
    obj.class.fields.forEach(function (field) {
      try {
        var val = obj.field(field.name).value;
        var s = '';
        try {
          s = val && val.content !== undefined ? val.content : String(val);
        } catch (e) {
          s = String(val);
        }
        if (field.name.toLowerCase().indexOf('key') >= 0 ||
            field.name.toLowerCase().indexOf('encrypt') >= 0 ||
            field.name.toLowerCase().indexOf('path') >= 0 ||
            field.name.toLowerCase().indexOf('ver') >= 0) {
          log('字段 ' + field.name + ' = ' + s);
        }
      } catch (e) {}
    });
  } catch (e) {
    log('枚举字段失败: ' + e);
  }
}

function mainIl2Cpp() {
  if (typeof Il2Cpp === 'undefined') {
    log('错误: 未加载 frida-il2cpp-bridge。');
    log('请使用: frida -U -f com.zygames.dungeon4 -l node_modules/frida-il2cpp-bridge/dist/index.js -l tools/frida_hook_seckey_il2cpp.js');
    return;
  }

  Il2Cpp.perform(function () {
    log('IL2CPP attach ok');

    function resolveUpdater() {
      var names = ['TJ.Updater', 'Updater'];
      for (var i = 0; i < names.length; i++) {
        try {
          var k = Il2Cpp.domain.assembly('Assembly-CSharp').image.class(names[i]);
          log('找到类 ' + names[i]);
          return k;
        } catch (e) {}
      }
      // 暴力搜
      Il2Cpp.domain.assemblies.forEach(function (asm) {
        try {
          asm.image.classes.forEach(function (klass) {
            if (klass.name.indexOf('Updater') >= 0 || klass.name.indexOf('BuildTime') >= 0) {
              log('候选类 ' + asm.name + ' :: ' + klass.namespace + '.' + klass.name);
            }
          });
        } catch (e) {}
      });
      return null;
    }

    var Updater = resolveUpdater();
    if (Updater) {
      try {
        var getBtd = Updater.method('GetBuildTimeData');
        Interceptor.attach(getBtd.virtualAddress, {
          onLeave: function (retval) {
            try {
              var obj = new Il2Cpp.Object(retval);
              log('GetBuildTimeData 返回');
              dumpBuildTimeData(obj);
            } catch (e) {
              log('解析 GetBuildTimeData 返回值失败: ' + e);
            }
          }
        });
        log('已 hook GetBuildTimeData');
      } catch (e) {
        log('hook GetBuildTimeData 失败: ' + e);
      }
    }

    // Sec.Pack1Decode
    try {
      var Sec = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('Sec');
      Sec.methods.forEach(function (m) {
        if (m.name.indexOf('Pack1Decode') !== 0 && m.name.indexOf('Pack0Decode') !== 0) return;
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            // 静态方法参数索引因是否有 hidden this 而异，多试几个
            this.candidates = [];
            for (var i = 0; i < 4; i++) {
              try {
                var o = new Il2Cpp.Object(args[i]);
                if (o.class && o.class.name === 'String') {
                  this.candidates.push(o.content);
                }
              } catch (e) {}
            }
          },
          onLeave: function (retval) {
            if (this.candidates && this.candidates.length) {
              log(m.name + ' 字符串参数: ' + JSON.stringify(this.candidates));
              // 通常 [0]=密文 Base64, [1]=password(secKey)
              if (this.candidates.length >= 2) {
                var pw = this.candidates[1];
                if (pw && pw.length >= 4 && pw.length <= 64 && this.candidates[0].length > 32) {
                  log('疑似 secKey/password = ' + JSON.stringify(pw));
                }
              }
            }
          }
        });
        log('已 hook Sec::' + m.name);
      });

      // MsgKey 属性
      try {
        var getMk = Sec.method('get_MsgKey');
        Interceptor.attach(getMk.virtualAddress, {
          onLeave: function (retval) {
            try {
              var s = new Il2Cpp.String(retval);
              log('Sec.MsgKey = ' + JSON.stringify(s.content));
            } catch (e) {}
          }
        });
      } catch (e) {}
    } catch (e) {
      log('hook Sec 失败: ' + e);
    }

    // 定时主动拉取一次 BuildTimeData（进入主城/加载表后更易成功）
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (tries > 60) {
        clearInterval(timer);
        return;
      }
      try {
        if (!Updater) return;
        var getBtd = Updater.method('GetBuildTimeData');
        var ret = getBtd.invoke();
        if (ret && !ret.isNull()) {
          log('主动 GetBuildTimeData 成功 (try ' + tries + ')');
          dumpBuildTimeData(ret);
          clearInterval(timer);
        }
      } catch (e) {}
    }, 2000);
  });
}

// 延迟等待 il2cpp 初始化
setTimeout(mainIl2Cpp, 1000);
log('脚本已加载，等待 IL2CPP…');
