// SPDX-License-Identifier: Apache-2.0
package com.ppgl.analyze.auth;

import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
class LoginCookieRecoveryTests {
    @Autowired MockMvc mvc;
    private Cookie stale() { return new Cookie(SecurityConfig.AUTH_COOKIE, "stale.invalid.token"); }
    @Test void staleCookieDoesNotBlockCredentialValidation() throws Exception {
        mvc.perform(post("/api/auth/login").cookie(stale()).contentType("application/json")
                .content("{\"username\":\"missing-cookie-regression-user\",\"password\":\"not-a-real-password\"}"))
                .andExpect(status().isBadRequest());
    }
    @Test void staleCookieDoesNotBlockSetupStatus() throws Exception {
        mvc.perform(get("/api/auth/setup-status").cookie(stale())).andExpect(status().isOk());
    }
    @Test void staleCookieStillCannotAccessProtectedData() throws Exception {
        mvc.perform(get("/api/ai/traces").cookie(stale())).andExpect(status().isUnauthorized());
    }
    @Test void explicitInvalidHeaderIsStillRejected() throws Exception {
        mvc.perform(post("/api/auth/login").cookie(stale()).header("Authorization", "Bearer stale.invalid.token")
                .contentType("application/json").content("{}"))
                .andExpect(status().isUnauthorized());
    }
}
