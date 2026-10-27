# Methods called from the dashboard page through addJavascriptInterface
-keepclassmembers class com.sreeram.filebridge.MainActivity$Bridge {
    @android.webkit.JavascriptInterface <methods>;
}
-keepattributes JavascriptInterface
