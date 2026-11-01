# 参赛安全合同（W06/W07，2026-10-12）

验收：错误验证/越权三次持久锁定；模型超时不计安全违规。银行最终提交检查锁、当日累计和精确操作凭证；验证失败不得改变银行账本，审计必须保留。重启不解锁、不降级；模型无验证或解锁工具。

`Service(db_path, clock=..., security_policy=None)` 兼容旧接口。`SecurityPolicy(mode='simulation'|'independent', verifier=TotpPasswordVerifier(path)|None)` 是受信启动配置，不由工具参数指定。数据库保存模式；首次默认simulation。independent模式禁止静默降级，未绑定验证器红色操作关闭。

新增可信动作：`verify_operation {operation_id, expected_summary, password, otp}` 返回verification_token/expiry，仅可信UI持有。`confirm` 增加可选verification_token；凭证绑定U1、操作ID、完整summary哈希、有效期，单次消费。重复成功confirm返回原回执。`security_unlock {password,otp,reason}` 要求独立双因素、非空原因；simulation仅允许开发显式配置的验证器解除，不能一键解锁。`security_status {}` 只返回模式/是否绑定/绑定来源/锁定状态/计数，不返回秘密。

独立因素适配器：`TotpPasswordVerifier(path)` 只读取指定独立SQLite凭据库，口令scrypt摘要；RFC6238 SHA1六位30秒TOTP、±1窗口、接受计数器单次使用。离线`enroll(path,password,base32_secret,provenance='USER_ENROLLED'|'TEST_FIXTURE')` 一次绑定，拒绝覆盖。测试绑定必须显式标识，不能冒称用户真实注册。认证库不提供给DSH、不进入交付包；宿主管理员仍为受信边界。口令+OTP不具抗钓鱼能力，不宣称NIST AAL2认证。

新增表security_state、verification_grants、security_audit；普通业务audit同步记录。审计只存动作/错误码/对象/摘要哈希，禁止口令、OTP、token。失败回滚后单独持久化违规，记录失败不提交部分业务修改；资金提交事务内部再次检查。

LOCKED时允许只读、拒绝、独立解锁；阻止prepare、confirm、注入资金、安排关联/修改。既有成功操作查询/重复确认不会重复执行。锁定不因时间或重启自动解除。3次违规的窗口15分钟，锁定保持至受控解除。

参考：NIST SP800-63B-4 https://pages.nist.gov/800-63-4/sp800-63b.html ；RFC6238 https://datatracker.ietf.org/doc/html/rfc6238 。本实现仅复用算法和限次/单次凭证原则，不声称符合其全部要求。

W08隔离探针使用现有Docker Engine及镜像，证据另存security/isolation-probe.json；工具白名单不等于OS沙箱，DSH实际受限运行由root接入。

下一步我该怎么验证：临时新数据库执行tests/competition_security，核查密码/OTP错误、锁定、重启、重放、摘要变化、累计边界与并发后的账本及审计；不触及既有runtime。
