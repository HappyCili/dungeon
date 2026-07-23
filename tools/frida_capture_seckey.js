/**
 * 抓取配置表 Pack1 密钥（secKey / Pack1Decode 密码参数）
 * 依赖: frida-il2cpp-bridge（与本脚本一起 -l 加载）
 */
'use strict';

function log(msg) {
  console.log('[secKey] ' + msg);
}

function csStr(v) {
  try {
    if (v === null || v === undefined) return null;
    if (v.isNull && v.isNull()) return null;
    if (typeof v === 'string') return v;
    if (v.content !== undefined) return v.content;
    return v.toString();
  } catch (e) {
    return null;
  }
}

function dumpObjectFields(obj, label) {
  if (!obj || (obj.isNull && obj.isNull())) {
    log(label + ' = null');
    return;
  }
  log(label + ' 类型: ' + obj.class.name);
  try {
    obj.class.fields.forEach(function (field) {
      try {
        var val = obj.field(field.name).value;
        var s = csStr(val);
        if (s === null) s = String(val);
        // 全部打出关键字段，key/path/ver 必打
        var n = field.name.toLowerCase();
        if (
          n.indexOf('key') >= 0 ||
          n.indexOf('encrypt') >= 0 ||
          n.indexOf('path') >= 0 ||
          n.indexOf('ver') >= 0 ||
          n.indexOf('url') >= 0 ||
          n.indexOf('channel') >= 0 ||
          n.indexOf('mode') >= 0
        ) {
          log(label + '.' + field.name + ' = ' + JSON.stringify(s));
        }
      } catch (e) {}
    });
  } catch (e) {
    log(label + ' 枚举字段失败: ' + e);
  }
  // 显式尝试常见名
  ['secKey', 'SecKey', 'encryptPaths', 'resVer', 'version', 'splitMode', 'updateInfoURL', 'downloadURL'].forEach(
    function (name) {
      try {
        var f = obj.tryField ? obj.tryField(name) : obj.field(name);
        if (!f) return;
        var v = f.value;
        log(label + '.' + name + ' = ' + JSON.stringify(csStr(v)));
      } catch (e) {}
    }
  );
}

function findClass(name) {
  var found = null;
  Il2Cpp.domain.assemblies.forEach(function (asm) {
    if (found) return;
    try {
      var c = asm.image.tryClass(name);
      if (c) found = c;
    } catch (e) {}
    if (found) return;
    try {
      asm.image.classes.forEach(function (klass) {
        if (found) return;
        var full = (klass.namespace ? klass.namespace + '.' : '') + klass.name;
        if (full === name || klass.name === name) found = klass;
      });
    } catch (e) {}
  });
  return found;
}

function hookPack1(secClass) {
  secClass.methods.forEach(function (m) {
    if (m.name.indexOf('Pack1Decode') !== 0 && m.name.indexOf('Pack0Decode') !== 0) return;
    try {
      m.implementation = function () {
        var args = Array.prototype.slice.call(arguments);
        var strs = [];
        for (var i = 0; i < args.length; i++) {
          var s = csStr(args[i]);
          if (s !== null) strs.push(s);
        }
        if (strs.length) {
          log(m.name + ' 参数字符串: ' + JSON.stringify(strs.map(function (s) {
            // 密文很长，截断
            return s.length > 80 ? s.slice(0, 40) + '…(len=' + s.length + ')' : s;
          })));
          // 启发式：较短的那个更像密钥
          strs.forEach(function (s) {
            if (s.length >= 4 && s.length <= 64 && /^[\x20-\x7e]+$/.test(s) && s.indexOf('{') !== 0) {
              // Base64 密文通常很长且无空格
              if (s.length <= 32 || !/^[A-Za-z0-9+/]+=*$/.test(s) || s.length < 100) {
                log('>>> 疑似密钥 password/secKey = ' + JSON.stringify(s));
              }
            }
          });
          // 双参数：第二个常为 key
          if (strs.length >= 2 && strs[1].length <= 64) {
            log('>>> Pack 密码候选[1] = ' + JSON.stringify(strs[1]));
          }
        }
        return m.invoke.apply(this, args);
      };
      log('已 hook ' + secClass.name + '::' + m.name + ' overload');
    } catch (e) {
      // 用 Interceptor 兜底
      try {
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            this.ss = [];
            for (var i = 0; i < 6; i++) {
              try {
                var o = new Il2Cpp.Object(args[i]);
                if (o.class && o.class.name === 'String') {
                  this.ss.push(o.content);
                }
              } catch (e2) {}
            }
          },
          onLeave: function () {
            if (this.ss && this.ss.length) {
              log(m.name + ' (native) 字符串: ' + JSON.stringify(this.ss.map(function (s) {
                return s.length > 80 ? s.slice(0, 40) + '…' : s;
              })));
              if (this.ss.length >= 2) {
                log('>>> Pack 密码候选[1] = ' + JSON.stringify(this.ss[1]));
              }
            }
          }
        });
        log('已 Interceptor.attach ' + m.name);
      } catch (e3) {
        log('hook ' + m.name + ' 失败: ' + e + ' / ' + e3);
      }
    }
  });
}

function tryDumpBuildTime() {
  var names = ['TJ.Updater', 'Updater'];
  for (var i = 0; i < names.length; i++) {
    var klass = findClass(names[i]);
    if (!klass) continue;
    log('找到类 ' + names[i]);
    klass.methods.forEach(function (m) {
      if (
        m.name.indexOf('BuildTime') >= 0 ||
        m.name.indexOf('GetBuild') >= 0 ||
        m.name === 'StartUp' ||
        m.name.indexOf('LoadBuild') >= 0
      ) {
        log('  方法: ' + m.name + ' params=' + m.parameterCount);
      }
    });
    try {
      var getBtd = klass.tryMethod('GetBuildTimeData') || klass.method('GetBuildTimeData');
      // hook 返回值
      Interceptor.attach(getBtd.virtualAddress, {
        onLeave: function (retval) {
          try {
            if (retval.isNull()) return;
            var obj = new Il2Cpp.Object(retval);
            dumpObjectFields(obj, 'BuildTimeData');
          } catch (e) {
            log('GetBuildTimeData onLeave 解析失败: ' + e);
          }
        }
      });
      log('已 hook GetBuildTimeData');
      // 主动 invoke（静态）
      try {
        var ret = getBtd.invoke();
        if (ret && !ret.isNull()) {
          dumpObjectFields(ret, 'BuildTimeData(主动)');
        }
      } catch (e) {
        log('主动 GetBuildTimeData 失败(可能尚未初始化): ' + e);
      }
    } catch (e) {
      log('处理 GetBuildTimeData 失败: ' + e);
    }
  }

  // 搜 BuildTimeData 类本身
  var btd = findClass('BuildTimeData') || findClass('TJ.BuildTimeData');
  if (btd) {
    log('BuildTimeData 字段列表:');
    btd.fields.forEach(function (f) {
      log('  field ' + f.name + ' : ' + f.type.name);
    });
  }
}

function main() {
  if (typeof Il2Cpp === 'undefined') {
    log('错误: Il2Cpp 未定义，请先 -l frida-il2cpp-bridge 的 index.js');
    return;
  }
  Il2Cpp.perform(function () {
    log('IL2CPP ready, unityVersion=' + (Il2Cpp.unityVersion || '?'));

    // 列出含 Sec / Updater 的类
    Il2Cpp.domain.assemblies.forEach(function (asm) {
      try {
        asm.image.classes.forEach(function (klass) {
          var n = klass.name;
          if (n === 'Sec' || n === 'Updater' || n.indexOf('BuildTime') >= 0) {
            log('类 ' + asm.name + ' :: ' + (klass.namespace || '') + '.' + n);
          }
        });
      } catch (e) {}
    });

    var sec = findClass('Sec');
    if (sec) {
      log('找到 Sec');
      hookPack1(sec);
      try {
        var getMk = sec.tryMethod('get_MsgKey');
        if (getMk) {
          Interceptor.attach(getMk.virtualAddress, {
            onLeave: function (retval) {
              try {
                if (!retval.isNull()) {
                  var s = new Il2Cpp.String(retval);
                  log('Sec.MsgKey = ' + JSON.stringify(s.content));
                }
              } catch (e) {}
            }
          });
        }
      } catch (e) {}
    } else {
      log('未找到 Sec 类');
    }

    tryDumpBuildTime();

    // 定时再 dump（资源加载后）
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (n > 90) {
        clearInterval(t);
        return;
      }
      try {
        var Updater = findClass('TJ.Updater') || findClass('Updater');
        if (!Updater) return;
        var getBtd = Updater.tryMethod('GetBuildTimeData');
        if (!getBtd) return;
        var ret = getBtd.invoke();
        if (ret && !ret.isNull()) {
          dumpObjectFields(ret, 'BuildTimeData(定时#' + n + ')');
          // 若已有 secKey 就停
          try {
            var sk = ret.field('secKey').value;
            var s = csStr(sk);
            if (s && s.length >= 4) {
              log('>>> 成功拿到 secKey = ' + JSON.stringify(s));
              clearInterval(t);
            }
          } catch (e) {}
        }
      } catch (e) {}
    }, 2000);
  });
}

setTimeout(main, 500);
log('capture 脚本已加载，等待 IL2CPP…');
