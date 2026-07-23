/**
 * 从运行中的客户端抓取配置表解密密钥 secKey / Pack1 密码。
 *
 * 包名: com.zygames.dungeon4
 *
 * 用法（设备已 root 或用可调试包 + frida-server）:
 *   frida -U -f com.zygames.dungeon4 -l tools/frida_hook_seckey.js --no-pause
 *   frida -U com.zygames.dungeon4 -l tools/frida_hook_seckey.js
 *
 * 若已装 frida-il2cpp-bridge，可改为加载 tools/frida_hook_seckey_il2cpp.js
 */

'use strict';

function log(msg) {
  console.log('[secKey-hook] ' + msg);
}

function tryHookIl2Cpp() {
  // 可选：全局已注入 Il2Cpp（frida-il2cpp-bridge）
  if (typeof Il2Cpp === 'undefined') {
    log('未检测到 Il2Cpp bridge，跳过 C# 层 hook（可改用 frida_hook_seckey_il2cpp.js）');
    return false;
  }

  Il2Cpp.perform(function () {
    log('Il2Cpp 已就绪，domain=' + Il2Cpp.domain.name);

    // 1) BuildTimeData / Updater.GetBuildTimeData
    var candidates = [
      { image: 'Assembly-CSharp', className: 'TJ.Updater', method: 'GetBuildTimeData' },
      { image: 'Assembly-CSharp', className: 'TJ.Updater', method: 'get_Instance' },
    ];

    candidates.forEach(function (c) {
      try {
        var klass = Il2Cpp.domain.assembly(c.image).image.class(c.className);
        var methods = klass.methods.filter(function (m) {
          return m.name.indexOf('BuildTime') >= 0 || m.name.indexOf('secKey') >= 0 ||
            m.name === 'GetBuildTimeData' || m.name.indexOf('Encrypt') >= 0;
        });
        methods.forEach(function (m) {
          log('发现方法 ' + c.className + '::' + m.name + ' (' + m.parameterCount + ')');
        });
      } catch (e) {
        log('查找 ' + c.className + ' 失败: ' + e);
      }
    });

    // 2) Sec.Pack1Decode(string data, string password)
    try {
      var sec = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('Sec');
      sec.methods.forEach(function (m) {
        if (m.name.indexOf('Pack1') >= 0 || m.name.indexOf('MsgKey') >= 0) {
          log('Sec::' + m.name);
        }
      });

      var pack1 = sec.method('Pack1Decode');
      // 可能有重载，逐个 attach
      sec.methods.forEach(function (m) {
        if (m.name !== 'Pack1Decode') return;
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            try {
              // 实例方法 this=args[0]；静态则从 0 起为参数（随 il2cpp 约定变化）
              this._dump = true;
            } catch (e) {}
          },
          onLeave: function (retval) {
            log('Sec.Pack1Decode 返回（请结合 il2cpp-bridge 打印 string 参数）');
          }
        });
        log('已 hook Sec.Pack1Decode @ ' + m.virtualAddress);
      });
    } catch (e) {
      log('hook Sec 失败: ' + e);
    }
  });
  return true;
}

function tryHookJava() {
  if (!Java.available) {
    log('Java 运行时不可用');
    return;
  }
  Java.perform(function () {
    log('Java 层已就绪');
    // 应用启动时可看 Application
    try {
      var ActivityThread = Java.use('android.app.ActivityThread');
      var app = ActivityThread.currentApplication();
      if (app) {
        log('当前包名: ' + app.getPackageName());
      }
    } catch (e) {}
  });
}

function tryHookNativeOpenSslDes() {
  // Node/OpenSSL 风格 DES 在部分构建中可能落到 native
  var names = ['EVP_EncryptInit_ex', 'DES_ecb_encrypt', 'DES_set_key'];
  names.forEach(function (name) {
    var addr = Module.findExportByName(null, name);
    if (addr) {
      log('导出 ' + name + ' @ ' + addr);
    }
  });
}

log('脚本加载，目标包 com.zygames.dungeon4');
tryHookJava();
tryHookNativeOpenSslDes();
// Il2Cpp bridge 需在启动脚本里 require；此处仅探测
setTimeout(function () {
  tryHookIl2Cpp();
}, 3000);

log('提示: 完整 C# 字段 dump 请使用 frida_hook_seckey_il2cpp.js + frida-il2cpp-bridge');
