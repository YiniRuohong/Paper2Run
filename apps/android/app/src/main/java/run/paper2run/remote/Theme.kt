package run.paper2run.remote

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/** The web UI's design tokens (apps/web/src/styles/tokens.css), so the native chrome matches the page. */
data class Palette(
    val bg: Color, val side: Color, val surface: Color, val line: Color,
    val text: Color, val muted: Color, val faint: Color,
    val accent: Color, val accentInk: Color, val ok: Color, val bad: Color,
)

val LightPalette = Palette(
    bg = Color(0xFFF7F7F8), side = Color(0xFFF1F1F3), surface = Color(0xFFFFFFFF), line = Color(0xFFE4E4E8),
    text = Color(0xFF1D1D1F), muted = Color(0xFF66666E), faint = Color(0xFFA6A6AD),
    accent = Color(0xFF5B5BD6), accentInk = Color(0xFFFFFFFF), ok = Color(0xFF136F35), bad = Color(0xFFBE2A1E),
)

val DarkPalette = Palette(
    bg = Color(0xFF1C1C1F), side = Color(0xFF19191C), surface = Color(0xFF222226), line = Color(0xFF2E2E34),
    text = Color(0xFFECECF0), muted = Color(0xFF94949C), faint = Color(0xFF5F5F67),
    accent = Color(0xFF8E8CFF), accentInk = Color(0xFF15151A), ok = Color(0xFF32D583), bad = Color(0xFFFF6B6B),
)

val LocalPalette = staticCompositionLocalOf { LightPalette }

@Composable
fun P2RTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val p = if (dark) DarkPalette else LightPalette
    val scheme = (if (dark) darkColorScheme() else lightColorScheme()).copy(
        primary = p.accent, onPrimary = p.accentInk, background = p.bg, onBackground = p.text,
        surface = p.bg, onSurface = p.text, onSurfaceVariant = p.muted, outline = p.line, error = p.bad,
        surfaceContainerHigh = p.surface,
    )
    androidx.compose.runtime.CompositionLocalProvider(LocalPalette provides p) {
        MaterialTheme(colorScheme = scheme, content = content)
    }
}
