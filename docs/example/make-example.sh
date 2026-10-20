#!/usr/bin/env bash
# Builds a small example repo to try local-review on (and to take the README screenshots): a Kotlin
# tip calculator with a `feature/split-bill` branch that touches code, tests, resources and project
# files.
#
#   docs/example/make-example.sh /tmp/tipcalc
#   cd /tmp/tipcalc && local-review serve --base main
set -euo pipefail

dir="${1:?usage: make-example.sh <new directory>}"
[ -e "$dir" ] && { echo "$dir already exists" >&2; exit 1; }
mkdir -p "$dir" && cd "$dir"
git init -q -b main
git config user.name "Example Dev"
git config user.email "dev@example.com"

mkdir -p src/main/kotlin/tips src/test/kotlin/tips src/main/res/values gradle

cat > settings.gradle.kts <<'EOF'
rootProject.name = "tipcalc"
EOF

cat > build.gradle.kts <<'EOF'
plugins {
    alias(libs.plugins.kotlin.jvm)
}

dependencies {
    testImplementation(libs.junit)
}
EOF

cat > gradle/libs.versions.toml <<'EOF'
[versions]
kotlin = "2.1.20"
junit = "4.13.2"

[libraries]
junit = { module = "junit:junit", version.ref = "junit" }

[plugins]
kotlin-jvm = { id = "org.jetbrains.kotlin.jvm", version.ref = "kotlin" }
EOF

cat > src/main/kotlin/tips/TipCalculator.kt <<'EOF'
package tips

import java.math.BigDecimal
import java.math.RoundingMode

/** Works out the tip on a bill. */
class TipCalculator(private val defaultPercent: Int = 18) {

    fun tip(bill: BigDecimal, percent: Int = defaultPercent): BigDecimal {
        require(percent >= 0) { "percent must not be negative" }
        return bill.multiply(BigDecimal(percent)).divide(BigDecimal(100), 2, RoundingMode.HALF_UP)
    }

    fun total(bill: BigDecimal, percent: Int = defaultPercent): BigDecimal = bill + tip(bill, percent)
}
EOF

cat > src/test/kotlin/tips/TipCalculatorTest.kt <<'EOF'
package tips

import java.math.BigDecimal
import org.junit.Assert.assertEquals
import org.junit.Test

class TipCalculatorTest {
    private val calculator = TipCalculator()

    @Test
    fun `tips the default percent`() {
        assertEquals(BigDecimal("9.00"), calculator.tip(BigDecimal("50.00")))
    }
}
EOF

cat > src/main/res/values/strings.xml <<'EOF'
<resources>
    <string name="app_name">Tip Calculator</string>
    <string name="tip_label">Tip</string>
    <string name="total_label">Total</string>
</resources>
EOF

git add -A && git commit -q -m "Tip calculator"

git checkout -q -b feature/split-bill

cat > src/main/kotlin/tips/TipCalculator.kt <<'EOF'
package tips

import java.math.BigDecimal
import java.math.RoundingMode

/** Works out the tip on a bill, and how to split the total between people. */
class TipCalculator(private val defaultPercent: Int = 18) {

    fun tip(bill: BigDecimal, percent: Int = defaultPercent): BigDecimal {
        require(percent in 0..100) { "percent must be between 0 and 100" }
        return bill.multiply(BigDecimal(percent)).divide(HUNDRED, 2, RoundingMode.HALF_UP)
    }

    fun total(bill: BigDecimal, percent: Int = defaultPercent): BigDecimal = bill + tip(bill, percent)

    /** The total split [people] ways; any leftover cents go to the first shares. */
    fun split(bill: BigDecimal, people: Int, percent: Int = defaultPercent): List<BigDecimal> {
        require(people > 0) { "people must be at least 1" }
        val cents = total(bill, percent).movePointRight(2).toLong()
        val share = cents / people
        val extra = (cents % people).toInt()
        return List(people) { i -> BigDecimal.valueOf(share + if (i < extra) 1 else 0, 2) }
    }

    private companion object {
        val HUNDRED = BigDecimal(100)
    }
}
EOF

cat > src/main/kotlin/tips/Split.kt <<'EOF'
package tips

import java.math.BigDecimal

/** One person's share of a split bill. */
data class Share(val person: Int, val amount: BigDecimal)

fun TipCalculator.shares(bill: BigDecimal, people: Int): List<Share> =
    split(bill, people).mapIndexed { i, amount -> Share(person = i + 1, amount = amount) }
EOF

cat > src/test/kotlin/tips/TipCalculatorTest.kt <<'EOF'
package tips

import java.math.BigDecimal
import org.junit.Assert.assertEquals
import org.junit.Test

class TipCalculatorTest {
    private val calculator = TipCalculator()

    @Test
    fun `tips the default percent`() {
        assertEquals(BigDecimal("9.00"), calculator.tip(BigDecimal("50.00")))
    }

    @Test
    fun `splits evenly`() {
        assertEquals(List(2) { BigDecimal("29.50") }, calculator.split(BigDecimal("50.00"), 2))
    }

    @Test
    fun `gives leftover cents to the first shares`() {
        val shares = calculator.split(BigDecimal("10.00"), 3, percent = 0)
        assertEquals(listOf("3.34", "3.33", "3.33").map(::BigDecimal), shares)
    }
}
EOF

cat > src/main/res/values/strings.xml <<'EOF'
<resources>
    <string name="app_name">Tip Calculator</string>
    <string name="tip_label">Tip</string>
    <string name="total_label">Total</string>
    <string name="split_label">Split between</string>
    <string name="share_label">Person %1$d pays %2$s</string>
</resources>
EOF

cat >> build.gradle.kts <<'EOF'

kotlin {
    jvmToolchain(17)
}
EOF

git add -A && git commit -q -m "Split the bill between people"
echo "Example repo ready: $dir (branch feature/split-bill). Run: cd $dir && local-review serve --base main"
