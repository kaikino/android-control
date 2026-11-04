// Fast UI hierarchy dumper for android-control.
//
// `uiautomator dump` waits (up to ~10 s, then fails) for the UI to go idle, so it breaks on screens
// with continuous animations. This runs the same on-device dumper classes from
// /system/framework/uiautomator.jar but with a short, non-fatal idle wait.
//
// Run on device:
//   CLASSPATH=/data/local/tmp/ac-dump.jar:/system/framework/uiautomator.jar \
//     app_process /system/bin AcDump [idle_timeout_ms] [out_file]
import android.app.UiAutomation;
import android.graphics.Point;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.File;
import java.lang.reflect.Method;
import java.util.concurrent.TimeoutException;

public class AcDump {
    public static void main(String[] args) throws Exception {
        long idleTimeout = args.length > 0 ? Long.parseLong(args[0]) : 1500;
        String out = args.length > 1 ? args[1] : "/data/local/tmp/_ac_ui.xml";

        Class<?> wrapperCls = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
        Object wrapper = wrapperCls.getConstructor().newInstance();
        wrapperCls.getMethod("connect").invoke(wrapper);
        try {
            UiAutomation ua = (UiAutomation) wrapperCls.getMethod("getUiAutomation").invoke(wrapper);
            try {
                ua.waitForIdle(150, idleTimeout);
            } catch (TimeoutException e) {
                System.err.println("WARN: UI not idle after " + idleTimeout + "ms; dumping anyway");
            }
            AccessibilityNodeInfo root = null;
            for (int i = 0; i < 20 && root == null; i++) {
                root = ua.getRootInActiveWindow();
                if (root == null) Thread.sleep(100);
            }
            if (root == null) {
                System.err.println("ERROR: null root node returned by UiTestAutomationBridge.");
                System.exit(2);
            }

            Class<?> dmgCls = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object dmg = dmgCls.getMethod("getInstance").invoke(null);
            Display display = (Display) dmgCls.getMethod("getRealDisplay", int.class).invoke(dmg, Display.DEFAULT_DISPLAY);
            Point size = new Point();
            display.getRealSize(size);
            int rotation = display.getRotation();

            Class<?> dumperCls = Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper");
            Method dump = dumperCls.getMethod("dumpWindowToFile", AccessibilityNodeInfo.class, File.class,
                    int.class, int.class, int.class);
            dump.invoke(null, root, new File(out), rotation, size.x, size.y);
            System.out.println("OK " + out);
        } finally {
            wrapperCls.getMethod("disconnect").invoke(wrapper);
        }
    }
}
