# The WebView bridge is reached by name from JavaScript, so it must survive
# shrinking even though nothing in Kotlin calls it.
-keepclassmembers class ae.aus.roadsense.MainActivity$Bridge {
    public *;
}
-keepattributes JavascriptInterface
