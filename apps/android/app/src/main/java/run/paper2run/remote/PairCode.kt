package run.paper2run.remote

/** Pairing codes are 8 ASCII digits: 3-digit nameplate + 5-digit secret, shown as NNN-NNNNN. */
object PairCode {
    fun normalize(input: String): String? {
        val digits = input.filter { it != '-' && it != ' ' }
        if (digits.length != 8 || !digits.all { it in '0'..'9' }) return null
        return digits.substring(0, 3) + "-" + digits.substring(3)
    }
}
